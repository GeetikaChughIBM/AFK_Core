"""
artifact_manager.py — Artifact collection, storage, and distribution.

Responsibilities:
  - Collect artifacts from worker VMs via SCP
  - Register artifacts in the state database
  - Distribute artifacts to other workers as inputs
  - Validate artifact presence and integrity (SHA-256)
  - Maintain a local artifact store for the Orchestrator
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from orchestrator.observability import get_logger
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager

log = get_logger("artifact_manager")

WORKER_WORKSPACE_BASE = "~/software-factory/workspace"
LOCAL_ARTIFACT_STORE = "artifacts"


class ArtifactManager:
    """
    Manages artifact lifecycle across the factory.

    Artifacts are collected from worker VMs after task completion
    and stored locally under artifacts/<task_id>/.
    """

    def __init__(
        self,
        worker_manager: WorkerManager,
        state: StateManager,
        local_store: str = LOCAL_ARTIFACT_STORE,
    ) -> None:
        self._wm = worker_manager
        self._state = state
        self._store = Path(local_store)
        self._store.mkdir(parents=True, exist_ok=True)

    # ── Collection ────────────────────────────────────────────────────────────

    def collect_artifacts(self, task_id: str) -> List[str]:
        """
        Download all artifacts produced by the agent for task_id.

        Looks for artifacts listed in handoff.json on the worker.
        Returns list of locally saved artifact paths.
        """
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            log.error("Cannot collect — task/worker not found", task_id=task_id)
            return []

        worker_id = task["worker_id"]
        workspace_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}"
        local_task_dir = self._store / task_id
        local_task_dir.mkdir(parents=True, exist_ok=True)

        # Try to read handoff.json to know what to collect
        handoff = self._read_handoff(worker_id, workspace_dir)
        artifact_paths = []

        if handoff and "artifacts" in handoff:
            artifact_list = handoff["artifacts"]
        else:
            # Fall back to collecting any .md / .json / .yaml in artifacts/
            artifact_list = [
                {"path": f"artifacts/{name}", "artifact_type": name.split(".")[0]}
                for name in self._list_remote_artifacts(worker_id, f"{workspace_dir}/artifacts")
            ]

        for art in artifact_list:
            remote_rel = art.get("path", "")
            if not remote_rel:
                continue

            remote_full = f"{workspace_dir}/{remote_rel}"
            local_name = Path(remote_rel).name
            local_path = str(local_task_dir / local_name)

            try:
                ssh = self._wm.get_connection(worker_id)
                ssh.download(remote_full, local_path)

                content_hash = self._hash_file(local_path)
                art_id = self._state.register_artifact(
                    task_id=task_id,
                    artifact_type=art.get("artifact_type", "unknown"),
                    path_on_worker=remote_full,
                    producing_agent=handoff.get("producing_agent", "") if handoff else "",
                    path_local=local_path,
                    content_hash=content_hash,
                )
                artifact_paths.append(local_path)
                log.info("Artifact collected",
                         task_id=task_id, artifact=local_name, local=local_path)
            except Exception as exc:
                log.error("Failed to collect artifact",
                          task_id=task_id, remote=remote_full, error=str(exc))

        # Also collect state.json
        try:
            ssh = self._wm.get_connection(worker_id)
            ssh.download(
                f"{workspace_dir}/state.json",
                str(local_task_dir / "state.json"),
            )
        except Exception:
            pass

        log.event("ARTIFACTS_COLLECTED",
                  f"Collected {len(artifact_paths)} artifacts for task {task_id}",
                  task_id=task_id, count=len(artifact_paths))
        return artifact_paths

    # ── Distribution ──────────────────────────────────────────────────────────

    def distribute_artifacts(
        self,
        source_task_id: str,
        target_task_id: str,
        artifact_types: Optional[List[str]] = None,
    ) -> bool:
        """
        Upload artifacts from source_task to the target_task's worker workspace.

        Used to pass Planner output → Executor input.
        """
        target_task = self._state.get_task(target_task_id)
        if not target_task or not target_task.get("worker_id"):
            log.error("Cannot distribute — target task/worker not found",
                      target_task_id=target_task_id)
            return False

        target_worker_id = target_task["worker_id"]
        ssh = self._wm.get_connection(target_worker_id)
        target_artifacts_dir = (
            f"{WORKER_WORKSPACE_BASE}/{target_task_id}/artifacts"
        )
        ssh.run_check(f"mkdir -p {target_artifacts_dir}")

        # Get locally stored artifacts from source task
        source_dir = self._store / source_task_id
        if not source_dir.exists():
            log.warning("No local artifacts for source task", task_id=source_task_id)
            return False

        artifacts = self._state.list_artifacts(source_task_id)
        distributed = 0
        for art in artifacts:
            if artifact_types and art["artifact_type"] not in artifact_types:
                continue
            local_path = art.get("path_local")
            if not local_path or not Path(local_path).exists():
                continue
            remote_path = f"{target_artifacts_dir}/{Path(local_path).name}"
            try:
                ssh.upload(local_path, remote_path)
                distributed += 1
                log.debug("Artifact distributed",
                          from_task=source_task_id, to_task=target_task_id,
                          artifact=Path(local_path).name)
            except Exception as exc:
                log.error("Failed to distribute artifact",
                          artifact=local_path, error=str(exc))

        log.event("ARTIFACTS_DISTRIBUTED",
                  f"Distributed {distributed} artifacts to task {target_task_id}",
                  source_task=source_task_id, target_task=target_task_id)
        return distributed > 0

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_required_artifacts(self, task_id: str, task_def: dict) -> bool:
        """
        Check that all required output artifacts are present locally.
        Returns True if all required artifacts exist and are non-empty.
        """
        outputs = task_def.get("outputs", [])
        task_dir = self._store / task_id
        all_present = True

        for output in outputs:
            if not output.get("required", False):
                continue
            artifact_path = task_dir / Path(output["path"]).name
            if not artifact_path.exists() or artifact_path.stat().st_size == 0:
                log.error("Required artifact missing or empty",
                          task_id=task_id, artifact=output["path"])
                all_present = False

        if all_present:
            log.info("All required artifacts present", task_id=task_id)
        return all_present

    def read_handoff_local(self, task_id: str) -> Optional[dict]:
        """Read the locally collected handoff.json for a task."""
        handoff_path = self._store / task_id / "handoff.json"
        if not handoff_path.exists():
            return None
        try:
            with open(handoff_path) as f:
                return json.load(f)
        except Exception as exc:
            log.error("Could not read local handoff.json",
                      task_id=task_id, error=str(exc))
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_handoff(self, worker_id: str, workspace_dir: str) -> Optional[dict]:
        try:
            ssh = self._wm.get_connection(worker_id)
            content = ssh.read_remote_file(f"{workspace_dir}/artifacts/handoff.json")
            return json.loads(content)
        except Exception:
            return None

    def _list_remote_artifacts(self, worker_id: str, remote_dir: str) -> List[str]:
        try:
            ssh = self._wm.get_connection(worker_id)
            code, out, _ = ssh.run(f"ls {remote_dir} 2>/dev/null")
            if code == 0:
                return [f for f in out.splitlines() if f.strip()]
            return []
        except Exception:
            return []

    @staticmethod
    def _hash_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
