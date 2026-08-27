"""
ssh_client.py — SSH abstraction layer for the Software Factory.

Wraps paramiko to provide:
  - Password and key-based authentication
  - Command execution (blocking, with timeout)
  - tmux session management
  - SCP file transfer (upload/download)
  - Connection pooling per worker

All callers use SSHClient.run() and SSHClient.upload() — the transport
implementation can be swapped without touching the rest of the codebase.
"""

import io
import os
import time
from pathlib import Path
from typing import Optional, Tuple

import paramiko
from scp import SCPClient

from orchestrator.observability import get_logger

log = get_logger("ssh")


class SSHCommandError(Exception):
    """Raised when an SSH command exits with a non-zero status and check=True."""
    def __init__(self, command: str, exit_code: int, stdout: str, stderr: str):
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"SSH command exited {exit_code}: {command!r}\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )


class SSHClient:
    """
    Manages a persistent SSH connection to a single worker VM.

    Usage:
        client = SSHClient(host="vm.example.com", username="geetika", password="...")
        client.connect()
        exit_code, out, err = client.run("ls ~/software-factory")
        client.disconnect()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        port: int = 22,
        connect_timeout: int = 30,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.key_path = key_path
        self.port = port
        self.connect_timeout = connect_timeout
        self._client: Optional[paramiko.SSHClient] = None

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Open SSH connection. Raises on failure."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.connect_timeout,
        }
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        elif self.password:
            kwargs["password"] = self.password
        else:
            raise ValueError("Either password or key_path must be provided")

        log.debug("Connecting to SSH host", host=self.host, user=self.username, port=self.port)
        client.connect(**kwargs)
        self._client = client
        log.info("SSH connection established", host=self.host)

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            log.debug("SSH connection closed", host=self.host)

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def ensure_connected(self) -> None:
        if not self.is_connected():
            self.connect()

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ── Command execution ─────────────────────────────────────────────────────

    def run(
        self,
        command: str,
        timeout: int = 60,
        check: bool = False,
        env: Optional[dict] = None,
    ) -> Tuple[int, str, str]:
        """
        Execute a command over SSH.

        Returns: (exit_code, stdout, stderr)
        Raises SSHCommandError if check=True and exit_code != 0.
        """
        self.ensure_connected()

        if env:
            env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
            command = f"export {env_prefix} && {command}"

        log.debug("SSH run", host=self.host, command=command[:120])
        _, stdout_obj, stderr_obj = self._client.exec_command(command, timeout=timeout)

        # Wait for completion
        channel = stdout_obj.channel
        channel.settimeout(timeout)
        stdout_data = stdout_obj.read().decode("utf-8", errors="replace")
        stderr_data = stderr_obj.read().decode("utf-8", errors="replace")
        exit_code = channel.recv_exit_status()

        log.debug(
            "SSH command complete",
            host=self.host, exit_code=exit_code,
            stdout_len=len(stdout_data), stderr_len=len(stderr_data),
        )

        if check and exit_code != 0:
            raise SSHCommandError(command, exit_code, stdout_data, stderr_data)

        return exit_code, stdout_data, stderr_data

    def run_check(self, command: str, timeout: int = 60, env: Optional[dict] = None) -> str:
        """Run command, raise SSHCommandError on non-zero exit, return stdout."""
        _, stdout, _ = self.run(command, timeout=timeout, check=True, env=env)
        return stdout

    # ── tmux management ───────────────────────────────────────────────────────

    def tmux_has_session(self, session: str) -> bool:
        """Return True if a tmux session with this name exists on the remote."""
        code, _, _ = self.run(f"tmux has-session -t {session} 2>/dev/null")
        return code == 0

    def tmux_new_session(self, session: str) -> None:
        """Create a new detached tmux session. No-op if it already exists."""
        if self.tmux_has_session(session):
            log.debug("tmux session already exists", session=session, host=self.host)
            return
        self.run_check(f"tmux new-session -d -s {session}")
        log.info("tmux session created", session=session, host=self.host)

    def tmux_send_keys(self, session: str, keys: str) -> None:
        """Send keys (a command string) to a tmux session."""
        escaped = keys.replace("'", "'\\''")
        self.run_check(f"tmux send-keys -t {session} '{escaped}' Enter")
        log.debug("tmux send-keys", session=session, host=self.host, keys=keys[:80])

    def tmux_capture_pane(self, session: str, lines: int = 200) -> str:
        """
        Capture the last N lines of tmux pane output.
        Returns raw text; caller is responsible for parsing.
        """
        code, out, _ = self.run(
            f"tmux capture-pane -t {session} -p -S -{lines} 2>/dev/null"
        )
        return out if code == 0 else ""

    def tmux_kill_session(self, session: str) -> None:
        """Kill a tmux session if it exists."""
        if self.tmux_has_session(session):
            self.run(f"tmux kill-session -t {session}")
            log.info("tmux session killed", session=session, host=self.host)

    # ── File transfer ─────────────────────────────────────────────────────────

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to the remote host via SCP."""
        self.ensure_connected()
        with SCPClient(self._client.get_transport()) as scp:
            scp.put(local_path, remote_path)
        log.debug("SCP upload complete", host=self.host,
                  local=local_path, remote=remote_path)

    def upload_content(self, content: str, remote_path: str) -> None:
        """Upload string content as a file to the remote host via SFTP."""
        self.ensure_connected()
        sftp = self._client.open_sftp()
        with sftp.open(remote_path, "w") as f:
            f.write(content)
        sftp.close()
        log.debug("SFTP write complete", host=self.host, remote=remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a remote file to a local path via SCP."""
        self.ensure_connected()
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with SCPClient(self._client.get_transport()) as scp:
            scp.get(remote_path, local_path)
        log.debug("SCP download complete", host=self.host,
                  remote=remote_path, local=local_path)

    def read_remote_file(self, remote_path: str) -> str:
        """Read a remote file's content as a string."""
        self.ensure_connected()
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as f:
                content = f.read().decode("utf-8", errors="replace")
        finally:
            sftp.close()
        return content

    def remote_file_exists(self, remote_path: str) -> bool:
        """Return True if the path exists on the remote VM."""
        code, _, _ = self.run(f"test -e {remote_path}")
        return code == 0
