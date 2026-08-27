"""
main.py — Software Factory Orchestrator CLI

Usage examples:
  python -m orchestrator.main init
  python -m orchestrator.main status
  python -m orchestrator.main bootstrap --worker worker-executor-01
  python -m orchestrator.main assign --task schema/task.yaml
  python -m orchestrator.main run --task TASK-001
  python -m orchestrator.main approve --task TASK-001
  python -m orchestrator.main reject  --task TASK-001 --reason "Tests incomplete"
  python -m orchestrator.main logs    --task TASK-001
"""

import json
import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from orchestrator.observability import init_logging, get_logger
from orchestrator.state import StateManager
from orchestrator.worker_manager import WorkerManager
from orchestrator.bootstrap_manager import BootstrapManager
from orchestrator.task_manager import TaskManager
from orchestrator.artifact_manager import ArtifactManager
from orchestrator.git_manager import GitManager
from orchestrator.gate_manager import GateManager

console = Console()


def _load_factory_config(path: str = "config/factory.yaml") -> dict:
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return yaml.safe_load(f)
    return {}


def _build_context(cfg: dict):
    """Construct all factory components with shared state."""
    obs_cfg = cfg.get("observability", {})
    init_logging(
        log_dir=obs_cfg.get("log_dir", "logs"),
        log_level=obs_cfg.get("log_level", "INFO"),
    )
    db_path = obs_cfg.get("state_db", "db/factory.db")
    state = StateManager(db_path=db_path)
    worker_mgr = WorkerManager(state=state)
    bootstrap_mgr = BootstrapManager(worker_manager=worker_mgr, state=state)
    task_mgr = TaskManager(worker_manager=worker_mgr, state=state)
    artifact_mgr = ArtifactManager(worker_manager=worker_mgr, state=state)
    git_mgr = GitManager(worker_manager=worker_mgr, state=state)
    gate_mgr = GateManager(worker_manager=worker_mgr, state=state)
    log = get_logger("main")
    return state, worker_mgr, bootstrap_mgr, task_mgr, artifact_mgr, git_mgr, gate_mgr, log


# ── CLI group ─────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Agentic Software Factory — Orchestrator CLI"""
    pass


# ── init ──────────────────────────────────────────────────────────────────────

@cli.command()
def init():
    """Initialise the factory database and sync the worker registry."""
    cfg = _load_factory_config()
    state, worker_mgr, *_ = _build_context(cfg)
    console.print(Panel(
        "[bold green]Software Factory initialised successfully.[/bold green]\n\n"
        "Database created. Worker registry synced.",
        title="[bold]init[/bold]",
    ))
    workers = worker_mgr.list_workers()
    console.print(f"[dim]Registered {len(workers)} worker(s).[/dim]")
    for w in workers:
        console.print(f"  [cyan]{w['id']}[/cyan]  {w['role']}  {w['host']}  [{w['status']}]")


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--worker", default=None, help="Show status for a specific worker")
@click.option("--task", default=None, help="Show status for a specific task")
def status(worker: Optional[str], task: Optional[str]):
    """Show factory, worker, and task status."""
    cfg = _load_factory_config()
    state, worker_mgr, *_ = _build_context(cfg)

    if worker:
        _show_worker(state, worker_mgr, worker)
    elif task:
        _show_task(state, task)
    else:
        _show_overview(state, worker_mgr)


def _show_overview(state: StateManager, worker_mgr: WorkerManager):
    # Workers table
    workers = worker_mgr.list_workers()
    wt = Table(title="Workers", show_header=True)
    wt.add_column("ID", style="cyan")
    wt.add_column("Role")
    wt.add_column("Host")
    wt.add_column("Status")
    wt.add_column("Current Task")
    for w in workers:
        status_color = {"ready": "green", "failed": "red", "executing": "yellow"}.get(w["status"], "white")
        wt.add_row(
            w["id"], w["role"], w["host"],
            f"[{status_color}]{w['status']}[/{status_color}]",
            w.get("current_task_id") or "-",
        )
    console.print(wt)

    # Recent tasks table
    tasks = state.list_tasks()[:10]
    tt = Table(title="Recent Tasks (last 10)", show_header=True)
    tt.add_column("ID", style="cyan")
    tt.add_column("Title")
    tt.add_column("Role")
    tt.add_column("Status")
    tt.add_column("Worker")
    tt.add_column("Branch")
    for t in tasks:
        status_color = {
            "completed": "green", "merged": "green",
            "failed": "red", "rejected": "red",
            "executing": "yellow", "awaiting_human_review": "yellow",
            "approved": "blue",
        }.get(t["status"], "white")
        tt.add_row(
            t["id"], (t.get("title") or "")[:40],
            t.get("agent_role", ""),
            f"[{status_color}]{t['status']}[/{status_color}]",
            t.get("worker_id") or "-",
            (t.get("branch") or "-")[:40],
        )
    console.print(tt)


def _show_worker(state: StateManager, worker_mgr: WorkerManager, worker_id: str):
    w = state.get_worker(worker_id)
    if not w:
        console.print(f"[red]Worker not found: {worker_id}[/red]")
        return
    console.print(Panel(
        "\n".join([
            f"[bold]ID[/bold]:        {w['id']}",
            f"[bold]Host[/bold]:      {w['host']}",
            f"[bold]Role[/bold]:      {w['role']}",
            f"[bold]Status[/bold]:    [cyan]{w['status']}[/cyan]",
            f"[bold]Task[/bold]:      {w.get('current_task_id') or '-'}",
            f"[bold]Bootstrap[/bold]: {w.get('bootstrap_name') or '-'}",
            f"[bold]Git User[/bold]:  {w.get('git_user_name') or '-'}",
        ]),
        title=f"[bold]Worker: {worker_id}[/bold]",
    ))
    # Recent events
    events = state.list_events(worker_id=worker_id, limit=10)
    if events:
        et = Table(title="Recent Events", show_header=True)
        et.add_column("Time")
        et.add_column("Event")
        et.add_column("Message")
        for e in events:
            et.add_row(e["ts"][:19], e["event_type"], (e.get("message") or "")[:60])
        console.print(et)


def _show_task(state: StateManager, task_id: str):
    t = state.get_task(task_id)
    if not t:
        console.print(f"[red]Task not found: {task_id}[/red]")
        return
    console.print(Panel(
        "\n".join([
            f"[bold]ID[/bold]:       {t['id']}",
            f"[bold]Title[/bold]:    {t.get('title', '')}",
            f"[bold]Role[/bold]:     {t.get('agent_role', '')}",
            f"[bold]Status[/bold]:   [cyan]{t['status']}[/cyan]",
            f"[bold]Worker[/bold]:   {t.get('worker_id') or '-'}",
            f"[bold]Branch[/bold]:   {t.get('branch') or '-'}",
            f"[bold]Retries[/bold]:  {t.get('retry_count', 0)}/{t.get('max_retries', 2)}",
            f"[bold]Error[/bold]:    {t.get('error_message') or '-'}",
            f"[bold]Created[/bold]:  {(t.get('created_at') or '')[:19]}",
        ]),
        title=f"[bold]Task: {task_id}[/bold]",
    ))
    # Gates
    gates = state.list_gates(task_id)
    if gates:
        gt = Table(title="Gates", show_header=True)
        gt.add_column("Gate")
        gt.add_column("Status")
        gt.add_column("Required")
        for g in gates:
            sc = "green" if g["status"] == "pass" else "red"
            gt.add_row(
                g["gate_name"],
                f"[{sc}]{g['status'].upper()}[/{sc}]",
                "YES" if g["required"] else "no",
            )
        console.print(gt)
    # Artifacts
    arts = state.list_artifacts(task_id)
    if arts:
        at = Table(title="Artifacts", show_header=True)
        at.add_column("Type")
        at.add_column("Local Path")
        at.add_column("Hash (short)")
        for a in arts:
            at.add_row(
                a["artifact_type"],
                (a.get("path_local") or "")[-50:],
                (a.get("content_hash") or "")[:12],
            )
        console.print(at)


# ── workers ───────────────────────────────────────────────────────────────────

@cli.command()
def workers():
    """List all registered workers and their current status."""
    cfg = _load_factory_config()
    state, worker_mgr, *_ = _build_context(cfg)
    _show_overview(state, worker_mgr)


@cli.command()
@click.argument("worker_id")
def health(worker_id: str):
    """Run a health check (SSH connectivity) on a worker VM."""
    cfg = _load_factory_config()
    _, worker_mgr, *_ = _build_context(cfg)
    healthy = worker_mgr.health_check(worker_id)
    if healthy:
        console.print(f"[bold green]✓ {worker_id} is reachable[/bold green]")
    else:
        console.print(f"[bold red]✗ {worker_id} is NOT reachable[/bold red]")
        sys.exit(1)


# ── bootstrap ─────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--worker", required=True, help="Worker ID to bootstrap")
@click.option("--force-reset", is_flag=True, default=False,
              help="Kill existing tmux sessions and re-bootstrap from scratch")
def bootstrap(worker: str, force_reset: bool):
    """Prepare a worker VM for development (run DB2 build)."""
    cfg = _load_factory_config()
    state, worker_mgr, bootstrap_mgr, *_ = _build_context(cfg)

    console.print(Panel(
        f"[bold]Bootstrapping worker [cyan]{worker}[/cyan][/bold]\n"
        f"force_reset={force_reset}\n\n"
        f"This may take 1–3 hours. tmux keeps the build alive on the VM.\n"
        f"You can safely close this terminal and reconnect later.\n"
        f"Run [bold cyan]python -m orchestrator.main status --worker {worker}[/bold cyan] to check progress.",
        title="[bold]bootstrap[/bold]",
    ))

    success = bootstrap_mgr.bootstrap_worker(worker, force_reset=force_reset)
    if success:
        console.print(f"\n[bold green]✓ Worker {worker} is now READY[/bold green]")
    else:
        console.print(f"\n[bold red]✗ Bootstrap FAILED for {worker}[/bold red]")
        sys.exit(1)


@cli.command("build-output")
@click.option("--worker", required=True, help="Worker ID")
def build_output(worker: str):
    """Show current tmux build output from a worker VM."""
    cfg = _load_factory_config()
    _, worker_mgr, bootstrap_mgr, *_ = _build_context(cfg)
    output = bootstrap_mgr.get_build_output(worker)
    if output:
        console.print(Panel(output, title=f"Build output — {worker}"))
    else:
        console.print(f"[dim]No tmux output available for {worker}[/dim]")


# ── assign ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--task", "task_file", required=True, help="Path to task YAML file")
@click.option("--worker", default=None, help="Force assignment to specific worker ID")
def assign(task_file: str, worker: Optional[str]):
    """Register a task and assign it to a worker."""
    cfg = _load_factory_config()
    state, worker_mgr, bootstrap_mgr, task_mgr, *_ = _build_context(cfg)

    task_def = task_mgr.load_task(task_file)
    task_id = task_mgr.create_task(task_def)

    assigned = task_mgr.assign_task(task_id, worker_id=worker)
    if not assigned:
        console.print(f"[red]Could not assign task {task_id} — no available worker[/red]")
        sys.exit(1)

    t = state.get_task(task_id)
    console.print(Panel(
        f"[bold green]Task {task_id} assigned[/bold green]\n\n"
        f"Worker:  [cyan]{t.get('worker_id')}[/cyan]\n"
        f"Branch:  [cyan]{t.get('branch')}[/cyan]\n\n"
        f"Run: [bold cyan]python -m orchestrator.main run --task {task_id}[/bold cyan]",
        title="[bold]assign[/bold]",
    ))


# ── run ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--task", "task_id", required=True, help="Task ID to execute")
def run(task_id: str):
    """
    Execute the full factory pipeline for a task:
      workspace prep → git setup → Bob execution → artifact collection → gates → approval request
    """
    cfg = _load_factory_config()
    state, worker_mgr, bootstrap_mgr, task_mgr, artifact_mgr, git_mgr, gate_mgr, log = _build_context(cfg)

    task = state.get_task(task_id)
    if not task:
        console.print(f"[red]Task not found: {task_id}[/red]")
        sys.exit(1)

    task_def = yaml.safe_load(task.get("definition") or "{}")

    console.print(Panel(
        f"[bold]Running task [cyan]{task_id}[/cyan][/bold]\n"
        f"Worker:  [cyan]{task.get('worker_id')}[/cyan]\n"
        f"Branch:  [cyan]{task.get('branch')}[/cyan]",
        title="[bold]run[/bold]",
    ))

    # ── Step 1: Prepare workspace ──────────────────────────────────────────────
    console.print("[dim]Step 1: Preparing workspace...[/dim]")
    if not task_mgr.prepare_workspace(task_id):
        console.print("[red]✗ Workspace preparation failed[/red]")
        sys.exit(1)

    # ── Step 2: Setup git repo and task branch ─────────────────────────────────
    console.print("[dim]Step 2: Cloning repository and creating task branch...[/dim]")
    repo_url = task_def.get("repository", {}).get("url", "")
    base_branch = task_def.get("repository", {}).get("base_branch", "main")
    if not repo_url:
        console.print("[yellow]Warning: No repository URL in task definition — skipping git setup[/yellow]")
    else:
        if not git_mgr.setup_repo(task_id, repo_url, base_branch):
            console.print("[red]✗ Git repository setup failed[/red]")
            sys.exit(1)

    # ── Step 3: Start Bob on the worker ───────────────────────────────────────
    console.print("[dim]Step 3: Launching Bob agent on worker...[/dim]")
    assigned_worker_id = task.get("worker_id")
    if not assigned_worker_id:
        console.print("[red]✗ Task has no assigned worker — cannot execute[/red]")
        sys.exit(1)
    state.set_task_status(task_id, "executing")
    worker_mgr.set_status(assigned_worker_id, "executing", task_id=task_id)

    bob_launched = _launch_bob(task_id, task_def, state, worker_mgr, cfg)
    if not bob_launched:
        console.print("[yellow]Warning: Bob launch returned failure — will still poll state.json[/yellow]")

    # ── Step 4: Poll for agent completion ─────────────────────────────────────
    console.print("[dim]Step 4: Waiting for agent to complete...[/dim]")
    timeout = task_def.get("policy", {}).get("timeout_seconds", 3600)
    completed = task_mgr.poll_task_completion(task_id, timeout_seconds=timeout)
    if not completed:
        console.print("[red]✗ Task failed or timed out[/red]")
        retry = task_mgr.handle_failure(task_id)
        if retry:
            console.print(f"[yellow]Task queued for retry. Run again after worker is READY.[/yellow]")
        sys.exit(1)

    # ── Step 5: Collect artifacts ──────────────────────────────────────────────
    console.print("[dim]Step 5: Collecting artifacts from worker...[/dim]")
    artifacts = artifact_mgr.collect_artifacts(task_id)
    console.print(f"[dim]  Collected {len(artifacts)} artifact(s)[/dim]")

    # ── Step 6: Evaluate gates ─────────────────────────────────────────────────
    console.print("[dim]Step 6: Evaluating gates...[/dim]")
    gates_passed = gate_mgr.evaluate_all_gates(task_id)
    gate_mgr.print_gate_summary(task_id)

    if not gates_passed:
        console.print("[red]✗ One or more required gates failed[/red]")
        sys.exit(1)

    # ── Step 7: Request human approval ────────────────────────────────────────
    console.print("[dim]Step 7: All automated gates passed — requesting human approval...[/dim]")
    gate_mgr.request_human_approval(task_id)

    # Mark worker available (use already-resolved worker_id, not re-read task dict)
    worker_mgr.set_status(assigned_worker_id, "ready")

    console.print(
        f"\n[bold green]✓ Task {task_id} completed automated pipeline.[/bold green]\n"
        f"[yellow]Awaiting human approval before merge.[/yellow]"
    )


def _launch_bob(
    task_id: str,
    task_def: dict,
    state: StateManager,
    worker_mgr: WorkerManager,
    cfg: dict,
) -> bool:
    """
    Launch Bob on the worker VM via SSH.

    Bob is invoked with the task instructions file.
    The bob_api_key is injected as BOB_SHELL_API_KEY env var.
    Workers run Bob non-interactively: Bob reads the instructions.md
    and executes without interactive prompts.

    Note on Bob automation model:
      Currently Bob does not have a fully documented non-interactive flag.
      The factory's approach is:
        1. Inject BOB_SHELL_API_KEY as env var on the worker
        2. Bob reads the agent instructions.md as its initial context
        3. The task definition is structured so Bob can execute deterministically
      This is the minimum-permission approach: each worker has exactly one scoped key.
      Future: add --headless or --non-interactive flag when Bob exposes it.
    """
    task = state.get_task(task_id)
    if not task or not task.get("worker_id"):
        return False

    worker_id = task["worker_id"]
    bob_api_key = worker_mgr.get_bob_api_key(worker_id)
    if not bob_api_key:
        get_logger("main").warning("No Bob API key for worker — Bob may not start",
                                   worker_id=worker_id)

    workspace_dir = f"~/software-factory/workspace/{task_id}"
    repo_dir = f"{workspace_dir}/repo"
    instructions = f"{workspace_dir}/instructions.md"
    log_file = f"{workspace_dir}/logs/bob.log"

    role = task.get("agent_role", "executor")

    try:
        ssh = worker_mgr.get_connection(worker_id)

        # Create a tmux session for Bob execution
        bob_session = f"sf-bob-{task_id}"
        ssh.tmux_new_session(bob_session)

        # Build the bob command.
        # stdout/stderr are tee'd to the log file so both the tmux pane (visible
        # to any operator who attaches) and the log file receive all output.
        bob_cmd = (
            f"export BOB_SHELL_API_KEY='{bob_api_key}' && "
            f"cd {repo_dir} && "
            f"bob --context {instructions} --task-dir {workspace_dir} "
            f"2>&1 | tee {log_file}"
        )

        ssh.tmux_send_keys(bob_session, bob_cmd)

        # Store the job_id so the execute job is visible in the state DB
        job_id = state.create_job(
            task_id=task_id,
            worker_id=worker_id,
            job_type="execute",
            tmux_session=bob_session,
        )
        state.set_job_status(job_id, "running")
        get_logger("main").event("BOB_LAUNCHED",
                                  f"Bob launched on {worker_id} for task {task_id}",
                                  task_id=task_id, worker_id=worker_id,
                                  session=bob_session, job_id=job_id)
        return True
    except Exception as exc:
        get_logger("main").error("Failed to launch Bob",
                                  task_id=task_id, worker_id=worker_id, error=str(exc))
        return False


# ── approve / reject ──────────────────────────────────────────────────────────

@cli.command()
@click.option("--task", "task_id", required=True, help="Task ID to approve")
@click.option("--approver", default="human", help="Approver identifier")
def approve(task_id: str, approver: str):
    """Grant human approval for a task (clears it for merge)."""
    cfg = _load_factory_config()
    state, worker_mgr, _, task_mgr, artifact_mgr, git_mgr, gate_mgr, log = _build_context(cfg)
    gate_mgr.approve_task(task_id, approver=approver)


@cli.command()
@click.option("--task", "task_id", required=True, help="Task ID to reject")
@click.option("--reason", default="", help="Rejection reason")
@click.option("--rejector", default="human", help="Rejector identifier")
def reject(task_id: str, reason: str, rejector: str):
    """Reject a task at the human approval gate."""
    cfg = _load_factory_config()
    _, worker_mgr, _, task_mgr, artifact_mgr, git_mgr, gate_mgr, log = _build_context(cfg)
    gate_mgr.reject_task(task_id, reason=reason, rejector=rejector)


# ── logs ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--task", "task_id", default=None, help="Filter events by task ID")
@click.option("--worker", "worker_id", default=None, help="Filter events by worker ID")
@click.option("--limit", default=50, help="Number of events to show")
def logs(task_id: Optional[str], worker_id: Optional[str], limit: int):
    """Show structured audit log events."""
    cfg = _load_factory_config()
    state, *_ = _build_context(cfg)
    events = state.list_events(task_id=task_id, worker_id=worker_id, limit=limit)
    if not events:
        console.print("[dim]No events found[/dim]")
        return
    t = Table(title="Factory Audit Log", show_header=True)
    t.add_column("Time", style="dim")
    t.add_column("Event", style="cyan")
    t.add_column("Task")
    t.add_column("Worker")
    t.add_column("Message")
    for e in events:
        t.add_row(
            e["ts"][:19],
            e["event_type"],
            e.get("task_id") or "-",
            e.get("worker_id") or "-",
            (e.get("message") or "")[:70],
        )
    console.print(t)


# ── gates ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--task", "task_id", required=True, help="Task ID to show gate summary for")
def gates(task_id: str):
    """Show gate evaluation summary for a task."""
    cfg = _load_factory_config()
    state, worker_mgr, _, task_mgr, _, _, gate_mgr, log = _build_context(cfg)
    gate_mgr.print_gate_summary(task_id)


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
