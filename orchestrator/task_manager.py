"""
task_manager.py — Task assignment, tracking, and lifecycle management.

Responsibilities:
  - Load and validate task definitions
  - Assign tasks to available workers
  - Set up worker workspace for the task
  - Configure git identity on the worker
  - Inject Bob API key into worker environment
  - Monitor task execution state
  - Handle timeouts and retries
"""

import json
import time
from pathlib import Path
from typing import Optional

import yaml

from orchestrator.observability import get_logger
from orchestrator.ssh_client import SSHCommandError
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager

log = get_logger("task_manager")

WORKER_WORKSPACE_BASE = "~/software-factory/workspace"
WORKER_ARTIFACTS_BASE = "~/software-factory/artifacts"
_FACTORY_CONFIG_PATH = "config/factory.yaml"


class TaskManager:
    """
    Manages task lifecycle: creation, assignment, workspace setup, monitoring.
    """

    def __init__(
        self,
        worker_manager: WorkerManager,
        state: StateManager,
        factory_config_path: str = _FACTORY_CONFIG_PATH,
    ) -> None:
        self._wm = worker_manager
        self._state = state
        self._cfg = self._load_factory_config(factory_config_path)

    def _load_factory_config(self, path: str) -> dict:
        p = Path(path)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
        return {}

    # ── Public API ────────────────────────────────────────────────────────────

    def load_task(self, task_file: str) -> dict:
        """Load and return a task definition from a YAML file."""
        path = Path(task_file)
        if not path.exists():
            raise FileNotFoundError(f"Task file not found: {path}")
        with open(path) as f:
            task = yaml.safe_load(f)
        log.info("Task definition loaded", task_id=task.get("task_id"), file=str(path))
        return task

    def create_task(self, task_def: dict) -> str:
        """Register a task in the state database. Returns task_id."""
        task_id = self._state.create_task(task_def)
        log.event("TASK_CREATED", f"Task {task_id} registered",
                  task_id=task_id, title=task_def.get("title", ""))
        return task_id

    def assign_task(self, task_id: str, worker_id: Optional[str] = None) -> bool:
        """
        Assign a task to a worker VM.

        If worker_id is not specified, auto-selects the first available worker
        with the required role.

        Returns True on successful assignment.
        """
        task = self._state.get_task(task_id)
        if not task:
            log.error("Task not found", task_id=task_id)
            return False

        role = task.get("agent_role", "executor")

        if not worker_id:
            worker_id = self._wm.find_available_worker(role)
            if not worker_id:
                log.warning("No available worker for role", role=role, task_id=task_id)
                return False

        worker = self._wm.get_worker_def(worker_id)
        if not worker:
            log.error("Worker not found", worker_id=worker_id)
            return False

        worker_status = self._wm.get_status(worker_id)
        if worker_status != "ready":
            log.warning(
                "Worker not in READY state — cannot assign task",
                worker_id=worker_id, status=worker_status,
            )
            return False

        # Determine branch name — use configured prefix, default "factory"
        prefix = self._cfg.get("git", {}).get("task_branch_prefix", "factory")
        branch = f"{prefix}/{task_id}"

        # Update state
        self._state.set_task_status(
            task_id, "assigned",
            worker_id=worker_id, branch=branch,
        )
        self._wm.set_status(worker_id, "assigned", task_id=task_id)

        log.event("TASK_ASSIGNED", f"Task {task_id} assigned to {worker_id}",
                  task_id=task_id, worker_id=worker_id, branch=branch)
        return True

    def prepare_workspace(self, task_id: str) -> bool:
        """
        Prepare the worker workspace for task execution:
          1. Create workspace directories on worker
          2. Upload task definition
          3. Upload input artifacts
          4. Configure git identity
          5. Inject Bob API key as env var
        """
        task = self._state.get_task(task_id)
        if not task:
            log.error("Task not found in state", task_id=task_id)
            return False

        worker_id = task.get("worker_id")
        if not worker_id:
            log.error("Task has no assigned worker", task_id=task_id)
            return False

        worker = self._wm.get_worker_def(worker_id)
        if not worker:
            log.error("Worker definition not found", worker_id=worker_id)
            return False

        try:
            ssh = self._wm.get_connection(worker_id)
            workspace_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}"
            artifacts_dir = f"{workspace_dir}/artifacts"
            logs_dir = f"{workspace_dir}/logs"

            # Create directory structure
            ssh.run_check(f"mkdir -p {workspace_dir} {artifacts_dir} {logs_dir}")

            # Upload task definition
            task_def_json = json.dumps(yaml.safe_load(task["definition"]), indent=2)
            ssh.upload_content(task_def_json, f"{workspace_dir}/task.json")

            # Upload agent instructions
            role = task.get("agent_role", "executor")
            instructions_path = Path(f"agents/{role}/instructions.md")
            if instructions_path.exists():
                ssh.upload(
                    str(instructions_path),
                    f"{workspace_dir}/instructions.md",
                )

            # Configure git identity on worker for this task
            git_name = worker.get("git", {}).get("user_name", "Factory Agent")
            git_email = worker.get("git", {}).get("user_email", "factory@example.com")
            ssh.run_check(f'git config --global user.name "{git_name}"')
            ssh.run_check(f'git config --global user.email "{git_email}"')

            # Write state.json initial state
            initial_state = json.dumps({
                "agent": role,
                "task_id": task_id,
                "status": "ASSIGNED",
                "workspace": workspace_dir,
            }, indent=2)
            ssh.upload_content(initial_state, f"{workspace_dir}/state.json")

            log.info("Workspace prepared", task_id=task_id, worker_id=worker_id,
                     workspace=workspace_dir)
            return True

        except SSHCommandError as exc:
            log.error("SSH error preparing workspace",
                      task_id=task_id, command=exc.command, exit_code=exc.exit_code)
            return False
        except Exception as exc:
            log.error("Exception preparing workspace", task_id=task_id, error=str(exc))
            return False

    def read_worker_state(self, task_id: str) -> Optional[dict]:
        """
        Read the state.json from the worker's task workspace.
        Used to detect agent completion without polling Bob.
        """
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            return None

        worker_id = task["worker_id"]
        workspace_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}"
        try:
            ssh = self._wm.get_connection(worker_id)
            content = ssh.read_remote_file(f"{workspace_dir}/state.json")
            return json.loads(content)
        except Exception as exc:
            log.debug("Could not read worker state.json",
                      task_id=task_id, error=str(exc))
            return None

    def poll_task_completion(
        self,
        task_id: str,
        timeout_seconds: int = 3600,
        poll_interval: int = 20,
    ) -> bool:
        """
        Poll state.json on the worker until the agent reports COMPLETED or FAILED.
        Reconnects SSH automatically if the connection drops during a long task.
        Returns True on success, False on failure or timeout.
        """
        task = self._state.get_task(task_id)
        worker_id = (task or {}).get("worker_id")

        log.info("Polling for task completion",
                 task_id=task_id, worker_id=worker_id, timeout=timeout_seconds)
        start = time.time()

        while True:
            elapsed = time.time() - start
            if elapsed > timeout_seconds:
                log.error("Task polling timed out", task_id=task_id)
                self._state.set_task_status(task_id, "failed",
                                            error_message="Timeout waiting for agent completion")
                return False

            try:
                worker_state = self.read_worker_state(task_id)
            except Exception as exc:
                # SSH dropped — reconnect and retry next tick
                log.warning("SSH error reading worker state — reconnecting",
                            task_id=task_id, error=str(exc))
                if worker_id:
                    try:
                        self._wm.close_connection(worker_id)
                    except Exception:
                        pass
                time.sleep(poll_interval)
                continue

            if worker_state:
                status = worker_state.get("status", "")
                if status == "COMPLETED":
                    validation_passed = worker_state.get("validation_passed", True)
                    if validation_passed:
                        self._state.set_task_status(task_id, "validating")
                        log.event("TASK_AGENT_COMPLETE",
                                  f"Agent reports COMPLETED for task {task_id}",
                                  task_id=task_id)
                        return True
                    else:
                        self._state.set_task_status(task_id, "failed",
                                                    error_message="Agent validation failed")
                        log.event("TASK_VALIDATION_FAILED",
                                  f"Agent validation failed for task {task_id}",
                                  task_id=task_id)
                        return False
                elif status == "FAILED":
                    error = worker_state.get("error_message", "Agent reported FAILED")
                    self._state.set_task_status(task_id, "failed", error_message=error)
                    log.event("TASK_AGENT_FAILED", f"Agent failed for task {task_id}",
                              task_id=task_id, error=error)
                    return False

            log.debug(f"Task still executing ({int(elapsed)}s)", task_id=task_id)
            time.sleep(poll_interval)

    def handle_failure(self, task_id: str) -> bool:
        """
        Handle a failed task: check retry count, re-queue or mark FAILED.
        Returns True if the task will be retried.
        """
        task = self._state.get_task(task_id)
        if not task:
            return False

        retry_count = self._state.increment_retry(task_id)
        max_retries = task.get("max_retries", 2)

        if retry_count <= max_retries:
            log.warning(f"Task failed — retrying ({retry_count}/{max_retries})",
                        task_id=task_id)
            self._state.set_task_status(task_id, "queued")
            if task.get("worker_id"):
                self._wm.set_status(task["worker_id"], "ready")
            return True
        else:
            log.error("Task exhausted retries — marking FAILED",
                      task_id=task_id, retries=retry_count)
            self._state.set_task_status(task_id, "failed",
                                        error_message=f"Exhausted {max_retries} retries")
            return False
