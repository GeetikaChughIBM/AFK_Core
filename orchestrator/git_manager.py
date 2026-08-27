"""
git_manager.py — Repository and branch lifecycle management.

Responsibilities:
  - Clone repository into worker task workspace
  - Create task branch from base branch
  - Verify branch protections are respected
  - Collect branch and commit information
  - Coordinate merge-readiness checks (not the merge itself — that is the Orchestrator)

The Orchestrator never merges directly — it verifies gates and records approval.
Actual merge into main requires a human approval step.
"""

from pathlib import Path
from typing import Optional

import yaml

from orchestrator.observability import get_logger
from orchestrator.ssh_client import SSHCommandError
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager

log = get_logger("git_manager")

WORKER_WORKSPACE_BASE = "~/software-factory/workspace"

# Branches the factory will never push to or auto-merge into
PROTECTED_BRANCHES = {"main", "master", "release"}


class GitManager:
    """
    Manages git operations on worker VMs.

    Workers clone their own copy of the repository per task.
    All commits are made by the worker's configured git identity,
    NOT by Bob and NOT by the Orchestrator's identity.
    """

    def __init__(
        self,
        worker_manager: WorkerManager,
        state: StateManager,
        factory_config_path: str = "config/factory.yaml",
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

    # ── Repository setup ──────────────────────────────────────────────────────

    def setup_repo(self, task_id: str, repo_url: str, base_branch: str = "main") -> bool:
        """
        Clone the repository on the worker and create the task branch.

        The repository is cloned into:
          ~/software-factory/workspace/<task_id>/repo/

        Returns True on success.
        """
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            log.error("Task/worker not found for git setup", task_id=task_id)
            return False

        worker_id = task["worker_id"]
        branch = task.get("branch") or f"factory/{task_id}"

        # Validate branch safety
        if not self._is_safe_branch(branch):
            log.error("Refusing to create protected branch",
                      task_id=task_id, branch=branch)
            return False

        workspace_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}"
        repo_dir = f"{workspace_dir}/repo"
        worker = self._wm.get_worker_def(worker_id)

        try:
            ssh = self._wm.get_connection(worker_id)

            # Remove stale repo if exists
            ssh.run(f"rm -rf {repo_dir}")

            # Clone
            log.info("Cloning repository", task_id=task_id, repo=repo_url,
                     branch=base_branch, worker_id=worker_id)
            ssh.run_check(
                f"git clone --branch {base_branch} --depth 50 {repo_url} {repo_dir}",
                timeout=300,
            )

            # Configure git identity in repo
            git_cfg = worker.get("git", {}) if worker else {}
            git_name = git_cfg.get("user_name", "Factory Agent")
            git_email = git_cfg.get("user_email", "factory@example.com")
            ssh.run_check(f'git -C {repo_dir} config user.name "{git_name}"')
            ssh.run_check(f'git -C {repo_dir} config user.email "{git_email}"')

            # Create task branch
            log.info("Creating task branch", task_id=task_id, branch=branch)
            ssh.run_check(
                f"git -C {repo_dir} checkout -b {branch}",
                timeout=30,
            )

            # Verify branch
            code, out, _ = ssh.run(f"git -C {repo_dir} branch --show-current")
            actual_branch = out.strip()
            if actual_branch != branch:
                log.error("Branch mismatch after checkout",
                          expected=branch, actual=actual_branch)
                return False

            log.event("GIT_REPO_READY",
                      f"Repo cloned and branch {branch} created for task {task_id}",
                      task_id=task_id, repo=repo_url, branch=branch, worker_id=worker_id)
            return True

        except SSHCommandError as exc:
            log.error("Git setup SSH error",
                      task_id=task_id, command=exc.command, exit_code=exc.exit_code,
                      stderr=exc.stderr[:300])
            return False
        except Exception as exc:
            log.error("Git setup exception", task_id=task_id, error=str(exc))
            return False

    # ── Branch information ────────────────────────────────────────────────────

    def get_commit_sha(self, task_id: str) -> Optional[str]:
        """Get the latest commit SHA on the task branch."""
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            return None
        worker_id = task["worker_id"]
        repo_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}/repo"
        try:
            ssh = self._wm.get_connection(worker_id)
            code, out, _ = ssh.run(f"git -C {repo_dir} rev-parse HEAD", timeout=15)
            return out.strip() if code == 0 else None
        except Exception:
            return None

    def verify_branch_pushed(self, task_id: str) -> bool:
        """
        Verify that the task branch exists on the remote.
        This confirms the worker pushed its changes.
        """
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            return False
        worker_id = task["worker_id"]
        branch = task.get("branch", f"factory/{task_id}")
        repo_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}/repo"
        try:
            ssh = self._wm.get_connection(worker_id)
            code, out, _ = ssh.run(
                f"git -C {repo_dir} ls-remote --heads origin {branch}",
                timeout=30,
            )
            pushed = code == 0 and branch in out
            if pushed:
                log.info("Branch confirmed on remote", task_id=task_id, branch=branch)
            else:
                log.warning("Branch not found on remote", task_id=task_id, branch=branch)
            return pushed
        except Exception as exc:
            log.error("Could not verify remote branch",
                      task_id=task_id, branch=branch, error=str(exc))
            return False

    def get_branch_diff_summary(self, task_id: str, base_branch: str = "main") -> str:
        """
        Return a summary of files changed on the task branch vs base.
        Used by the Orchestrator's gate manager for review.
        """
        task = self._state.get_task(task_id)
        if not task or not task.get("worker_id"):
            return ""
        worker_id = task["worker_id"]
        branch = task.get("branch", f"factory/{task_id}")
        repo_dir = f"{WORKER_WORKSPACE_BASE}/{task_id}/repo"
        try:
            ssh = self._wm.get_connection(worker_id)
            _, out, _ = ssh.run(
                f"git -C {repo_dir} diff --name-status {base_branch}...{branch}",
                timeout=30,
            )
            return out
        except Exception:
            return ""

    # ── Safety checks ─────────────────────────────────────────────────────────

    def _is_safe_branch(self, branch: str) -> bool:
        """Return True if the branch is safe to create/push to."""
        protected = PROTECTED_BRANCHES | set(
            self._cfg.get("security", {}).get("protected_branches", [])
        )
        prefix = self._cfg.get("security", {}).get("allowed_branch_prefix", "factory/")
        if branch in protected:
            return False
        if not branch.startswith(prefix):
            log.warning("Branch does not match required prefix",
                        branch=branch, required_prefix=prefix)
            return False
        return True
