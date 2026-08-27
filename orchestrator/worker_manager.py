"""
worker_manager.py — Worker VM lifecycle management for the Software Factory.

Responsibilities:
  - Load worker registry from workers.yaml
  - Resolve secrets from secrets.env
  - Provide SSH connections to workers
  - Track and transition worker states
  - Health-check workers
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dotenv import dotenv_values

from orchestrator.observability import get_logger
from orchestrator.ssh_client import SSHClient
from orchestrator.state import StateManager

log = get_logger("worker_manager")


class WorkerManager:
    """
    Manages worker VM registry, SSH connections, and lifecycle state.

    Workers are defined in config/workers.yaml (no secrets).
    Secrets are resolved from config/secrets.env at runtime.
    """

    def __init__(
        self,
        workers_config: str = "config/workers.yaml",
        secrets_file: str = "config/secrets.env",
        state: Optional[StateManager] = None,
    ) -> None:
        self._workers_config = workers_config
        self._secrets = self._load_secrets(secrets_file)
        self._state = state or StateManager()
        self._connections: Dict[str, SSHClient] = {}
        self._worker_defs: Dict[str, Dict] = {}
        self._load_workers()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _load_secrets(self, secrets_file: str) -> Dict[str, str]:
        path = Path(secrets_file)
        if path.exists():
            return dict(dotenv_values(path))
        log.warning(
            "Secrets file not found — using environment variables only",
            path=secrets_file,
        )
        return dict(os.environ)

    def _load_workers(self) -> None:
        path = Path(self._workers_config)
        if not path.exists():
            raise FileNotFoundError(f"Workers config not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        workers = data.get("workers", [])
        for w in workers:
            self._worker_defs[w["id"]] = w
            self._state.upsert_worker(w)

        log.info(f"Loaded {len(workers)} workers from registry", config=str(path))

    def reload_workers(self) -> None:
        """Reload the worker registry (e.g. after adding a new worker)."""
        self._worker_defs.clear()
        self._load_workers()

    # ── Secret resolution ─────────────────────────────────────────────────────

    def _resolve_secret(self, secret_ref: str) -> Optional[str]:
        """Resolve a secret reference to its value from secrets.env or environment."""
        value = self._secrets.get(secret_ref) or os.environ.get(secret_ref)
        if not value:
            log.warning("Secret reference not found", ref=secret_ref)
        return value

    def get_ssh_password(self, worker_id: str) -> Optional[str]:
        w = self._worker_defs.get(worker_id)
        if not w:
            raise KeyError(f"Worker not found: {worker_id}")
        ref = w.get("ssh_password_secret_ref")
        if not ref:
            return None
        return self._resolve_secret(ref)

    def get_bob_api_key(self, worker_id: str) -> Optional[str]:
        w = self._worker_defs.get(worker_id)
        if not w:
            raise KeyError(f"Worker not found: {worker_id}")
        ref = w.get("bob_api_key_secret_ref")
        if not ref:
            return None
        return self._resolve_secret(ref)

    # ── SSH connections ───────────────────────────────────────────────────────

    def get_connection(self, worker_id: str) -> SSHClient:
        """
        Return a live SSH connection to the worker.
        Connections are cached; re-establishes if dropped.
        """
        if worker_id in self._connections:
            client = self._connections[worker_id]
            if client.is_connected():
                return client
            # Stale connection — rebuild
            log.warning("Stale SSH connection, reconnecting", worker_id=worker_id)
            del self._connections[worker_id]

        w = self._worker_defs.get(worker_id)
        if not w:
            raise KeyError(f"Worker not found in registry: {worker_id}")

        password = self.get_ssh_password(worker_id)
        client = SSHClient(
            host=w["host"],
            username=w["username"],
            password=password,
            port=w.get("port", 22),
        )
        client.connect()
        self._connections[worker_id] = client
        log.info("SSH connection opened", worker_id=worker_id, host=w["host"])
        return client

    def close_connection(self, worker_id: str) -> None:
        client = self._connections.pop(worker_id, None)
        if client:
            client.disconnect()

    def close_all_connections(self) -> None:
        for wid in list(self._connections.keys()):
            self.close_connection(wid)

    # ── Worker state ──────────────────────────────────────────────────────────

    def set_status(self, worker_id: str, status: str, task_id: Optional[str] = None) -> None:
        self._state.set_worker_status(worker_id, status, task_id)

    def get_status(self, worker_id: str) -> Optional[str]:
        w = self._state.get_worker(worker_id)
        return w["status"] if w else None

    def get_worker_def(self, worker_id: str) -> Optional[Dict]:
        return self._worker_defs.get(worker_id)

    def list_workers(self, role: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        return self._state.list_workers(role=role, status=status)

    def find_available_worker(self, role: str) -> Optional[str]:
        """
        Return the ID of the first available worker with the requested role,
        or None if no worker is available.
        """
        workers = self._state.list_workers(role=role, status="ready")
        if workers:
            return workers[0]["id"]
        return None

    # ── Health check ──────────────────────────────────────────────────────────

    def health_check(self, worker_id: str) -> bool:
        """
        Verify SSH connectivity to the worker.
        Returns True if reachable, False otherwise.
        """
        try:
            client = self.get_connection(worker_id)
            code, _, _ = client.run("echo SF_HEALTH_OK", timeout=10)
            healthy = code == 0
            if healthy:
                self._state.add_event("WORKER_HEALTH_CHECK", worker_id=worker_id,
                                      message="healthy")
            else:
                log.warning("Worker health check failed", worker_id=worker_id)
            return healthy
        except Exception as exc:
            log.error("Worker health check exception", worker_id=worker_id, error=str(exc))
            self._state.set_worker_status(worker_id, "offline")
            return False

    def health_check_all(self) -> Dict[str, bool]:
        """Run health_check on all registered workers. Returns {worker_id: bool}."""
        results = {}
        for wid in self._worker_defs:
            results[wid] = self.health_check(wid)
        return results
