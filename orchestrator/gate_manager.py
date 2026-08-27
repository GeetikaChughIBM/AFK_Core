"""
gate_manager.py — Automated gate evaluation and human approval workflow.

Gates define the transition conditions between task states.
Each gate must PASS before the task can advance to the next stage.

Gate sequence:
  ENVIRONMENT_READY → TASK_COMPLETE → TESTS_PASS →
  REVIEW_PASS (optional) → HUMAN_APPROVAL → MERGED

Human approval is the final non-automatable gate.
The Orchestrator pauses and waits for an explicit human `approve` command.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from orchestrator.observability import get_logger
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager

log = get_logger("gate_manager")
console = Console()

LOCAL_ARTIFACT_STORE = "artifacts"


class GateResult:
    def __init__(self, gate_name: str, passed: bool, details: str = ""):
        self.gate_name = gate_name
        self.passed = passed
        self.details = details

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"GateResult({self.gate_name}={status}: {self.details})"


class GateManager:
    """
    Evaluates automated gates and manages the human approval workflow.
    """

    def __init__(
        self,
        worker_manager: WorkerManager,
        state: StateManager,
        factory_config_path: str = "config/factory.yaml",
        artifact_store: str = LOCAL_ARTIFACT_STORE,
    ) -> None:
        self._wm = worker_manager
        self._state = state
        self._artifact_store = Path(artifact_store)
        self._cfg = self._load_config(factory_config_path)
        self._gates_cfg = self._cfg.get("gates", {})

    def _load_config(self, path: str) -> dict:
        p = Path(path)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
        return {}

    # ── Gate evaluation ───────────────────────────────────────────────────────

    def evaluate_all_gates(self, task_id: str) -> bool:
        """
        Run all required gates for a task.
        Returns True only if all required gates pass.
        """
        task = self._state.get_task(task_id)
        if not task:
            log.error("Task not found for gate evaluation", task_id=task_id)
            return False

        task_def = json.loads(task.get("definition") or "{}")

        gates_to_run = [
            ("task_complete", self._gate_task_complete),
            ("tests_pass", self._gate_tests_pass),
            ("branch_pushed", self._gate_branch_pushed),
            ("required_artifacts", self._gate_required_artifacts),
        ]

        # Optionally add review gate if enabled
        review_cfg = self._gates_cfg.get("review_pass", {})
        if review_cfg.get("required", False):
            gates_to_run.append(("review_pass", self._gate_review_pass))

        all_passed = True
        for gate_name, gate_fn in gates_to_run:
            try:
                result = gate_fn(task_id, task_def, task)
            except Exception as exc:
                result = GateResult(gate_name, False, f"Exception: {exc}")

            required = self._gates_cfg.get(gate_name, {}).get("required", True)
            status = "pass" if result.passed else "fail"
            self._state.record_gate(
                task_id=task_id,
                gate_name=gate_name,
                status=status if (result.passed or not required) else "fail",
                required=required,
                details={"detail": result.details},
            )

            if not result.passed:
                if required:
                    log.error(f"Required gate FAILED: {gate_name}",
                              task_id=task_id, details=result.details)
                    all_passed = False
                else:
                    log.warning(f"Optional gate skipped/failed: {gate_name}",
                                task_id=task_id)

        if all_passed:
            self._state.set_task_status(task_id, "awaiting_human_review")
            log.event("GATES_PASSED", f"All gates passed for task {task_id}",
                      task_id=task_id)
        else:
            self._state.set_task_status(task_id, "failed",
                                        error_message="One or more required gates failed")
            log.event("GATES_FAILED", f"Gate evaluation failed for task {task_id}",
                      task_id=task_id)

        return all_passed

    # ── Individual gate implementations ───────────────────────────────────────

    def _gate_task_complete(self, task_id: str, task_def: dict, task: dict) -> GateResult:
        """Verify agent reported COMPLETED with validation_passed=true."""
        state_file = self._artifact_store / task_id / "state.json"
        if not state_file.exists():
            return GateResult("task_complete", False, "state.json not found in local store")
        try:
            with open(state_file) as f:
                state = json.load(f)
            if state.get("status") != "COMPLETED":
                return GateResult("task_complete", False,
                                  f"Agent status is {state.get('status')!r}, expected COMPLETED")
            if not state.get("validation_passed", True):
                return GateResult("task_complete", False, "Agent reported validation_passed=false")
            return GateResult("task_complete", True, "Agent reported COMPLETED with validation_passed=true")
        except Exception as exc:
            return GateResult("task_complete", False, f"Could not parse state.json: {exc}")

    def _gate_tests_pass(self, task_id: str, task_def: dict, task: dict) -> GateResult:
        """Verify test-report.md exists and contains no FAIL markers."""
        test_report = self._artifact_store / task_id / "test-report.md"
        if not test_report.exists():
            return GateResult("tests_pass", False, "test-report.md not found")
        content = test_report.read_text()
        if "**Status**: FAIL" in content or "FAILED" in content.upper():
            return GateResult("tests_pass", False, "Test report contains FAIL entries")
        return GateResult("tests_pass", True, "No failures found in test report")

    def _gate_branch_pushed(self, task_id: str, task_def: dict, task: dict) -> GateResult:
        """Verify change-summary.json has a commit_sha."""
        summary_file = self._artifact_store / task_id / "change-summary.json"
        if not summary_file.exists():
            return GateResult("branch_pushed", False, "change-summary.json not found")
        try:
            with open(summary_file) as f:
                summary = json.load(f)
            sha = summary.get("commit_sha", "")
            if not sha:
                return GateResult("branch_pushed", False, "commit_sha is empty in change-summary.json")
            return GateResult("branch_pushed", True, f"commit_sha={sha[:12]}")
        except Exception as exc:
            return GateResult("branch_pushed", False, f"Could not parse change-summary.json: {exc}")

    def _gate_required_artifacts(self, task_id: str, task_def: dict, task: dict) -> GateResult:
        """Verify all required output artifacts exist and are non-empty."""
        outputs = task_def.get("outputs", [])
        task_dir = self._artifact_store / task_id
        missing = []
        for output in outputs:
            if not output.get("required", False):
                continue
            artifact_name = Path(output["path"]).name
            artifact_path = task_dir / artifact_name
            if not artifact_path.exists() or artifact_path.stat().st_size == 0:
                missing.append(artifact_name)
        if missing:
            return GateResult("required_artifacts", False,
                              f"Missing artifacts: {', '.join(missing)}")
        return GateResult("required_artifacts", True, "All required artifacts present")

    def _gate_review_pass(self, task_id: str, task_def: dict, task: dict) -> GateResult:
        """Extension point: check review-report.md for REVIEW_PASSED."""
        review_report = self._artifact_store / task_id / "review-report.md"
        if not review_report.exists():
            return GateResult("review_pass", False, "review-report.md not found (reviewer agent not run)")
        content = review_report.read_text()
        if "REVIEW_PASSED" in content:
            return GateResult("review_pass", True, "Review report indicates PASSED")
        return GateResult("review_pass", False, "Review report does not contain REVIEW_PASSED")

    # ── Human approval workflow ───────────────────────────────────────────────

    def request_human_approval(self, task_id: str) -> None:
        """
        Display approval request to the operator and record it in state.
        The task remains in AWAITING_HUMAN_REVIEW until approve() is called.
        """
        task = self._state.get_task(task_id)
        if not task:
            log.error("Task not found", task_id=task_id)
            return

        branch = task.get("branch", f"factory/{task_id}")
        worker_id = task.get("worker_id", "unknown")

        console.print()
        console.print(Panel(
            f"[bold yellow]Human approval required for task [cyan]{task_id}[/cyan][/bold yellow]\n\n"
            f"Branch:   [cyan]{branch}[/cyan]\n"
            f"Worker:   [cyan]{worker_id}[/cyan]\n"
            f"Status:   [yellow]AWAITING_HUMAN_REVIEW[/yellow]\n\n"
            f"Review the branch, then run:\n"
            f"  [bold green]python -m orchestrator.main approve --task {task_id}[/bold green]\n"
            f"  [bold red]python -m orchestrator.main reject --task {task_id}[/bold red]",
            title="[bold]Software Factory — Approval Required[/bold]",
            border_style="yellow",
        ))

        self._state.add_event(
            "HUMAN_APPROVAL_REQUESTED",
            task_id=task_id,
            message=f"Task {task_id} awaiting human approval on branch {branch}",
        )
        log.event("HUMAN_APPROVAL_REQUESTED",
                  f"Task {task_id} awaiting human approval",
                  task_id=task_id, branch=branch)

    def approve_task(self, task_id: str, approver: str = "human") -> bool:
        """Record human approval for a task."""
        task = self._state.get_task(task_id)
        if not task:
            log.error("Task not found for approval", task_id=task_id)
            return False

        if task["status"] != "awaiting_human_review":
            log.error(f"Task is not in AWAITING_HUMAN_REVIEW state",
                      task_id=task_id, status=task["status"])
            return False

        self._state.set_task_status(task_id, "approved")
        self._state.record_gate(
            task_id=task_id,
            gate_name="human_approval",
            status="pass",
            required=True,
            details={"approver": approver},
        )
        self._state.add_event(
            "HUMAN_APPROVAL_GRANTED",
            task_id=task_id,
            actor=approver,
            message=f"Task {task_id} approved by {approver}",
        )
        log.event("HUMAN_APPROVAL_GRANTED",
                  f"Task {task_id} approved by {approver}",
                  task_id=task_id, approver=approver)
        console.print(f"[bold green]✓ Task {task_id} approved.[/bold green]")
        return True

    def reject_task(self, task_id: str, reason: str = "", rejector: str = "human") -> bool:
        """Record human rejection for a task."""
        task = self._state.get_task(task_id)
        if not task:
            return False

        self._state.set_task_status(task_id, "rejected", error_message=reason)
        self._state.record_gate(
            task_id=task_id,
            gate_name="human_approval",
            status="fail",
            required=True,
            details={"rejector": rejector, "reason": reason},
        )
        self._state.add_event(
            "HUMAN_APPROVAL_REJECTED",
            task_id=task_id,
            actor=rejector,
            message=f"Task {task_id} rejected: {reason}",
        )
        log.event("HUMAN_APPROVAL_REJECTED",
                  f"Task {task_id} rejected",
                  task_id=task_id, reason=reason)
        console.print(f"[bold red]✗ Task {task_id} rejected.[/bold red]")
        return True

    # ── Gate summary display ──────────────────────────────────────────────────

    def print_gate_summary(self, task_id: str) -> None:
        """Print a formatted gate status table for a task."""
        gates = self._state.list_gates(task_id)
        if not gates:
            console.print(f"[dim]No gates recorded for task {task_id}[/dim]")
            return

        table = Table(title=f"Gate Summary — {task_id}", show_header=True)
        table.add_column("Gate", style="cyan")
        table.add_column("Status")
        table.add_column("Required")
        table.add_column("Evaluated At")
        table.add_column("Details")

        for g in gates:
            status_color = "green" if g["status"] == "pass" else "red"
            table.add_row(
                g["gate_name"],
                f"[{status_color}]{g['status'].upper()}[/{status_color}]",
                "YES" if g["required"] else "no",
                g.get("evaluated_at", "")[:19] or "-",
                str(g.get("details", ""))[:60],
            )
        console.print(table)
