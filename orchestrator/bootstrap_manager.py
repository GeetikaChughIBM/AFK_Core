"""
bootstrap_manager.py — VM provisioning and build lifecycle manager.

Responsibilities:
  - Copy bootstrap scripts to worker VMs
  - Start long-running builds inside job-scoped tmux sessions via job-wrapper.sh
  - Poll ~/software-factory/runtime/jobs/<job_id>/status for SUCCESS/FAILED/RUNNING
  - Fall back to tmux sentinel scan only when status file is absent (legacy compat)
  - Transition worker state through PROVISIONING → BOOTSTRAPPING → READY | FAILED
  - Handle SSH disconnections gracefully (tmux keeps processes alive)
"""

import time
from pathlib import Path
from typing import Optional

import yaml

from orchestrator.observability import get_logger
from orchestrator.ssh_client import SSHClient, SSHCommandError
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager

log = get_logger("bootstrap")

# Using ~/software-factory so no sudo is needed on the worker VM
_REMOTE_BOOTSTRAP_DIR = "~/software-factory/bootstrap"
_REMOTE_COMMON_SETUP = f"{_REMOTE_BOOTSTRAP_DIR}/common/setup.sh"
_REMOTE_WRAPPER = f"{_REMOTE_BOOTSTRAP_DIR}/job-wrapper.sh"
_LOCAL_WRAPPER = "bootstrap/job-wrapper.sh"


class BootstrapManager:
    """
    Manages the full lifecycle of preparing a worker VM for development.

    Workflow:
      1. SSH into worker
      2. Create workspace directories (common/setup.sh)
      3. Upload bootstrap scripts + job-wrapper.sh
      4. Allocate job_id in state DB
      5. Create job-scoped tmux session (sf-build-{job_id})
      6. Execute build via job-wrapper.sh inside tmux
      7. Poll ~/software-factory/runtime/jobs/{job_id}/status
      8. Mark worker READY or FAILED
    """

    def __init__(
        self,
        worker_manager: WorkerManager,
        state: StateManager,
        bootstrap_configs_dir: str = "bootstrap",
    ) -> None:
        self._wm = worker_manager
        self._state = state
        self._configs_dir = Path(bootstrap_configs_dir)

    # ── Public API ────────────────────────────────────────────────────────────

    def bootstrap_worker(
        self,
        worker_id: str,
        force_reset: bool = False,
    ) -> bool:
        """
        Prepare a worker VM for development.

        Returns True on success, False on failure.
        """
        worker = self._wm.get_worker_def(worker_id)
        if not worker:
            log.error("Worker not found", worker_id=worker_id)
            return False

        bootstrap_name = worker.get("bootstrap", "db2-v1216")
        config = self._load_bootstrap_config(bootstrap_name)
        if not config:
            log.error("Bootstrap config not found", name=bootstrap_name)
            return False

        current_status = self._wm.get_status(worker_id)
        if current_status == "ready" and not force_reset:
            log.info("Worker already READY — skipping bootstrap", worker_id=worker_id)
            return True

        if force_reset:
            log.info("Force reset requested — resetting worker state", worker_id=worker_id)
            self._reset_worker(worker_id, config)

        log.event("BOOTSTRAP_STARTED", f"Bootstrapping {worker_id}",
                  worker_id=worker_id, bootstrap=bootstrap_name)
        self._wm.set_status(worker_id, "provisioning")

        try:
            ssh = self._wm.get_connection(worker_id)
            self._run_common_setup(ssh, worker_id)
            self._upload_bootstrap_scripts(ssh, bootstrap_name, config)
            job_id = self._start_build(ssh, worker_id, config)
            success = self._poll_until_complete(ssh, worker_id, config, job_id)
        except SSHCommandError as exc:
            log.error("SSH command failed during bootstrap",
                      worker_id=worker_id, command=exc.command, exit_code=exc.exit_code)
            self._wm.set_status(worker_id, "failed")
            return False
        except Exception as exc:
            log.error("Bootstrap exception", worker_id=worker_id, error=str(exc))
            self._wm.set_status(worker_id, "failed")
            return False

        if success:
            self._wm.set_status(worker_id, "ready")
            log.event("BOOTSTRAP_COMPLETE", f"Worker {worker_id} is READY",
                      worker_id=worker_id, bootstrap=bootstrap_name)
            return True
        else:
            self._wm.set_status(worker_id, "failed")
            log.event("BOOTSTRAP_FAILED", f"Worker {worker_id} bootstrap FAILED",
                      worker_id=worker_id, bootstrap=bootstrap_name)
            return False

    def get_build_output(self, worker_id: str, config: Optional[dict] = None) -> str:
        """
        Return the latest build output for a worker.

        Reads stdout.log from the job runtime directory if available (preferred),
        otherwise falls back to tmux pane capture.
        """
        worker = self._wm.get_worker_def(worker_id)
        if not worker:
            return ""
        if config is None:
            name = worker.get("bootstrap", "db2-v1216")
            config = self._load_bootstrap_config(name) or {}

        try:
            ssh = self._wm.get_connection(worker_id)
            jobs_base = config.get("runtime", {}).get("jobs_base", "~/software-factory/runtime/jobs")
            session_prefix = config.get("tmux", {}).get("session_prefix", "sf-build")

            # Find the most recent bootstrap job for this worker
            job = self._state.get_latest_job(worker_id=worker_id, job_type="bootstrap")
            job_id = job["id"] if job else None

            # Preferred: read stdout.log written by job-wrapper.sh
            if job_id:
                content = self._try_read_remote_file(
                    ssh, f"{jobs_base}/{job_id}/stdout.log"
                )
                if content:
                    # Also show current step if available
                    step = self._try_read_remote_file(
                        ssh, f"{jobs_base}/{job_id}/current_step"
                    )
                    header = f"[job={job_id}  step={step.strip() if step else 'unknown'}]\n\n"
                    return header + content[-8000:]

            # Fallback: tmux pane capture (wrapper not yet started, or log not written)
            session = f"{session_prefix}-{job_id}" if job_id else f"{session_prefix}-unknown"
            pane = ssh.tmux_capture_pane(session, lines=500)
            if pane:
                return f"[job={job_id}  source=tmux-pane]\n\n{pane}"
            return ""
        except Exception as exc:
            log.error("Could not capture build output", worker_id=worker_id, error=str(exc))
            return ""

    # ── Internal steps ────────────────────────────────────────────────────────

    def _load_bootstrap_config(self, bootstrap_name: str) -> Optional[dict]:
        """Load bootstrap/db2/config.yaml (or equivalent) by bootstrap name."""
        # Map bootstrap name to directory, e.g. "db2-v1216" → bootstrap/db2/
        # Convention: directory is the part before the first dash after prefix
        parts = bootstrap_name.split("-")
        # Try exact sub-directory match first, then prefix match
        candidates = [
            self._configs_dir / parts[0] / "config.yaml",
            self._configs_dir / bootstrap_name / "config.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                with open(candidate) as f:
                    data = yaml.safe_load(f)
                return data
        log.error("Bootstrap config YAML not found", bootstrap_name=bootstrap_name,
                  tried=[str(c) for c in candidates])
        return None

    def _run_common_setup(self, ssh: SSHClient, worker_id: str) -> None:
        """Upload and run common/setup.sh on the worker."""
        log.info("Running common setup", worker_id=worker_id)

        # Create remote bootstrap dir
        ssh.run_check(f"mkdir -p {_REMOTE_BOOTSTRAP_DIR}/common")

        # Upload common setup script
        local_setup = str(self._configs_dir / "common" / "setup.sh")
        ssh.upload(local_setup, f"{_REMOTE_BOOTSTRAP_DIR}/common/setup.sh")
        ssh.run_check(f"chmod +x {_REMOTE_BOOTSTRAP_DIR}/common/setup.sh")

        # Run it
        code, out, err = ssh.run(
            f"bash {_REMOTE_BOOTSTRAP_DIR}/common/setup.sh",
            timeout=120,
        )
        if code != 0:
            log.warning("Common setup returned non-zero", worker_id=worker_id,
                        exit_code=code, stderr=err[:200])
        else:
            log.info("Common setup complete", worker_id=worker_id)

    def _upload_bootstrap_scripts(
        self, ssh: SSHClient, bootstrap_name: str, config: dict
    ) -> None:
        """Upload all bootstrap scripts for this environment to the worker."""
        script_relative = config.get("bootstrap", {}).get("script", "bootstrap/db2/commands.sh")
        script_local = Path(script_relative)
        if not script_local.exists():
            raise FileNotFoundError(f"Bootstrap script not found: {script_local}")

        # Determine remote directory
        remote_dir = f"{_REMOTE_BOOTSTRAP_DIR}/{script_local.parent.name}"
        ssh.run_check(f"mkdir -p {remote_dir}")

        # Upload build script
        remote_script = f"{remote_dir}/{script_local.name}"
        ssh.upload(str(script_local), remote_script)
        ssh.run_check(f"chmod +x {remote_script}")

        # Upload config
        config_local = script_local.parent / "config.yaml"
        if config_local.exists():
            ssh.upload(str(config_local), f"{remote_dir}/config.yaml")

        # Upload job-wrapper.sh
        wrapper_local = Path(_LOCAL_WRAPPER)
        if not wrapper_local.exists():
            raise FileNotFoundError(f"job-wrapper.sh not found at {_LOCAL_WRAPPER}")
        ssh.run_check(f"mkdir -p {_REMOTE_BOOTSTRAP_DIR}")
        ssh.upload(str(wrapper_local), _REMOTE_WRAPPER)
        ssh.run_check(f"chmod +x {_REMOTE_WRAPPER}")

        log.info("Bootstrap scripts uploaded", remote_dir=remote_dir)

    def _start_build(self, ssh: SSHClient, worker_id: str, config: dict) -> str:
        """
        Start the build inside a job-scoped tmux session via job-wrapper.sh.

        Session name: sf-build-{job_id}
        The wrapper writes status/exit_code/stdout.log under
        ~/software-factory/runtime/jobs/{job_id}/ so the Orchestrator can poll
        a file instead of parsing tmux pane text.
        """
        tmux_cfg = config.get("tmux", {})
        session_prefix = tmux_cfg.get("session_prefix", "sf-build")
        script_relative = config.get("bootstrap", {}).get("script", "bootstrap/db2/commands.sh")
        script_name = Path(script_relative).name
        remote_dir = f"{_REMOTE_BOOTSTRAP_DIR}/{Path(script_relative).parent.name}"
        remote_script = f"{remote_dir}/{script_name}"
        jobs_base = config.get("runtime", {}).get("jobs_base", "~/software-factory/runtime/jobs")

        # Inject env vars
        env_vars = config.get("env_vars", {})
        env_exports = " && ".join(f"export {k}={v}" for k, v in env_vars.items())

        self._wm.set_status(worker_id, "bootstrapping")

        # Allocate job record first so we can use job_id as the tmux session name
        job_id = self._state.create_job(
            task_id=None,
            worker_id=worker_id,
            job_type="bootstrap",
            tmux_session=None,  # filled in below
        )
        session = f"{session_prefix}-{job_id}"
        self._state.set_job_tmux_session(job_id, session)

        # Ensure runtime jobs directory exists on the worker
        ssh.run_check(f"mkdir -p {jobs_base}/{job_id}")

        # Create fresh tmux session (job-scoped, never reused)
        ssh.tmux_new_session(session)

        # Invoke the build via the job wrapper so lifecycle is written to files
        if env_exports:
            cmd = f"{env_exports} && bash {_REMOTE_WRAPPER} {job_id} {remote_script}"
        else:
            cmd = f"bash {_REMOTE_WRAPPER} {job_id} {remote_script}"
        ssh.tmux_send_keys(session, cmd)

        self._state.set_job_status(job_id, "running")
        log.event("BUILD_STARTED", f"Build started in tmux session {session}",
                  worker_id=worker_id, session=session, job_id=job_id,
                  jobs_base=f"{jobs_base}/{job_id}")
        return job_id

    def _poll_until_complete(
        self, ssh: SSHClient, worker_id: str, config: dict, job_id: str
    ) -> bool:
        """
        Poll ~/software-factory/runtime/jobs/{job_id}/status for SUCCESS/FAILED.

        Falls back to tmux pane sentinel scan if the status file is absent
        (e.g. wrapper failed to start).

        Returns True on success, False on failure/timeout.
        """
        tmux_cfg = config.get("tmux", {})
        session_prefix = tmux_cfg.get("session_prefix", "sf-build")
        session = f"{session_prefix}-{job_id}"
        poll_interval = tmux_cfg.get("poll_interval_seconds", 30)
        timeout_seconds = tmux_cfg.get("timeout_seconds", 14400)
        jobs_base = config.get("runtime", {}).get("jobs_base", "~/software-factory/runtime/jobs")
        status_file = f"{jobs_base}/{job_id}/status"
        sentinels = config.get("sentinels", {})
        success_sentinel = sentinels.get("success", "SF_BUILD_SUCCESS")
        failure_sentinel = sentinels.get("failure", "SF_BUILD_FAILED")

        log.info(
            "Polling for build completion",
            worker_id=worker_id, job_id=job_id, session=session,
            status_file=status_file, timeout=timeout_seconds, poll=poll_interval,
        )

        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout_seconds:
                log.error("Bootstrap timed out", worker_id=worker_id,
                          job_id=job_id, elapsed=int(elapsed))
                self._capture_and_log_output(ssh, session, worker_id, job_id,
                                             jobs_base, config)
                return False

            # Reconnect if needed
            try:
                ssh.ensure_connected()
            except Exception as exc:
                log.warning("SSH disconnected during poll — retrying",
                            worker_id=worker_id, error=str(exc))
                time.sleep(poll_interval)
                try:
                    self._wm.close_connection(worker_id)
                    ssh = self._wm.get_connection(worker_id)
                except Exception:
                    pass
                continue

            # Primary: read status file written by job-wrapper.sh.
            # Use try_read_remote_file to avoid a separate exists+read race
            # (two SSH calls where the file can disappear between them).
            status = self._try_read_remote_file(ssh, status_file)

            if status is not None:
                status = status.strip()
                if status == "SUCCESS":
                    log.info("Job status file: SUCCESS",
                             worker_id=worker_id, job_id=job_id)
                    self._state.set_job_status(job_id, "success", exit_code=0)
                    return True
                if status == "FAILED":
                    exit_code = self._read_exit_code(ssh, jobs_base, job_id)
                    log.error("Job status file: FAILED",
                              worker_id=worker_id, job_id=job_id, exit_code=exit_code)
                    self._capture_and_log_output(ssh, session, worker_id, job_id,
                                                 jobs_base, config)
                    self._state.set_job_status(job_id, "failed", exit_code=exit_code)
                    return False
                # status == "RUNNING" — keep polling, report current_step
                current_step = self._try_read_remote_file(
                    ssh, f"{jobs_base}/{job_id}/current_step"
                ) or "unknown"
                log.info(
                    f"Job RUNNING ({int(elapsed)}s/{timeout_seconds}s)",
                    worker_id=worker_id, job_id=job_id,
                    current_step=current_step.strip(),
                )
            else:
                # Status file not present yet — wrapper still starting up.
                # Fall back to tmux sentinel scan as a safety net.
                try:
                    output = ssh.tmux_capture_pane(session, lines=100)
                    if success_sentinel in output:
                        log.info("Fallback sentinel: SUCCESS",
                                 worker_id=worker_id, job_id=job_id)
                        return True
                    if failure_sentinel in output:
                        log.error("Fallback sentinel: FAILED",
                                  worker_id=worker_id, job_id=job_id)
                        return False
                except Exception:
                    pass
                log.debug(
                    f"Status file absent, waiting ({int(elapsed)}s)",
                    worker_id=worker_id, job_id=job_id,
                )

            time.sleep(poll_interval)

    def _try_read_remote_file(self, ssh: SSHClient, remote_path: str) -> Optional[str]:
        """
        Read a remote file and return its content, or None if the file does not
        exist or any error occurs. Never raises — safe to call in a poll loop.
        """
        try:
            return ssh.read_remote_file(remote_path)
        except Exception:
            return None

    def _read_exit_code(self, ssh: SSHClient, jobs_base: str, job_id: str) -> Optional[int]:
        try:
            raw = self._try_read_remote_file(ssh, f"{jobs_base}/{job_id}/exit_code")
            return int(raw.strip()) if raw else None
        except Exception:
            return None

    def _capture_and_log_output(
        self,
        ssh: SSHClient,
        session: str,
        worker_id: str,
        job_id: str,
        jobs_base: str,
        config: dict,
    ) -> None:
        # Try job log file first (more complete)
        try:
            log_file = f"{jobs_base}/{job_id}/stdout.log"
            if ssh.remote_file_exists(log_file):
                content = ssh.read_remote_file(log_file)
                log.error("Build log tail on failure",
                          worker_id=worker_id, job_id=job_id,
                          output=content[-3000:])
                return
        except Exception:
            pass
        # Fallback: tmux pane
        try:
            output = ssh.tmux_capture_pane(session, lines=500)
            log.error("Final tmux output on failure",
                      worker_id=worker_id, job_id=job_id, output=output[-2000:])
        except Exception:
            pass

    def _reset_worker(self, worker_id: str, config: dict) -> None:
        """Kill all sf-build-* tmux sessions and reset worker state before re-bootstrap."""
        session_prefix = config.get("tmux", {}).get("session_prefix", "sf-build")
        try:
            ssh = self._wm.get_connection(worker_id)
            # Kill all sessions matching the prefix (handles job-scoped names)
            code, out, _ = ssh.run(
                f"tmux list-sessions -F '#{{session_name}}' 2>/dev/null"
                f" | grep '^{session_prefix}-' | xargs -r -I{{}} tmux kill-session -t {{}}"
            )
            log.info("Killed bootstrap tmux sessions",
                     worker_id=worker_id, prefix=session_prefix)
        except Exception as exc:
            log.warning("Could not kill tmux sessions on reset",
                        worker_id=worker_id, error=str(exc))
        self._wm.set_status(worker_id, "registered")
