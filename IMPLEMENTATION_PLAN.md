# IMPLEMENTATION_PLAN.md — Incremental Implementation Guide

## Phase 1: Foundation (Now) ← You are here

**Goal**: End-to-end workflow working reliably with 2 workers.

### What is built

- [x] Factory project structure and configuration
- [x] Worker registry (`config/workers.yaml`)
- [x] Secrets model (`config/secrets.env.example`)
- [x] DB2 bootstrap scripts (`bootstrap/db2/`)
- [x] Common VM setup (`bootstrap/common/setup.sh`)
- [x] Agent instructions (`agents/planner/`, `agents/executor/`)
- [x] Task, artifact, handoff schemas (`schema/`)
- [x] `StateManager` — SQLite-backed persistent state
- [x] `SSHClient` — paramiko SSH abstraction
- [x] `WorkerManager` — VM registry + connections + secrets
- [x] `BootstrapManager` — tmux-based build monitor
- [x] `TaskManager` — task assignment + workspace setup
- [x] `ArtifactManager` — collect and distribute artifacts
- [x] `GitManager` — clone + branch + verify push
- [x] `GateManager` — automated gates + human approval
- [x] `main.py` — CLI (init, status, bootstrap, assign, run, approve, reject, logs, gates)
- [x] `SECURITY.md`, `ARCHITECTURE.md`

### Steps to get it running

1. **Copy secrets file**
   ```bash
   cp config/secrets.env.example config/secrets.env
   # Edit config/secrets.env — fill in actual passwords and Bob API keys
   ```

2. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Initialise the factory**
   ```bash
   python -m orchestrator.main init
   ```
   This creates `db/factory.db` and syncs the worker registry.

4. **Verify SSH connectivity**
   ```bash
   python -m orchestrator.main health worker-executor-01
   python -m orchestrator.main health worker-planner-01
   ```

5. **Bootstrap executor VM**
   ```bash
   python -m orchestrator.main bootstrap --worker worker-executor-01
   ```
   `~/db2` already exists on the Fyre VM. This SSHes in, uploads `bootstrap/db2/commands.sh`,
   starts it in a tmux session, and polls every 30 seconds until `SF_BUILD_SUCCESS` appears.
   Takes 1–3 hours depending on the VM.

   To watch progress live:
   ```bash
   python -m orchestrator.main build-output --worker worker-executor-01
   ```

6. **Create and assign a task**
   ```bash
   # Edit schema/task.yaml with a real repo URL and task
   python -m orchestrator.main assign --task schema/task.yaml --worker worker-executor-01
   ```

7. **Run the task pipeline**
   ```bash
   python -m orchestrator.main run --task TASK-001
   ```
   This:
   - Prepares workspace on worker
   - Clones repo + creates task branch
   - Launches Bob on worker via tmux
   - Polls state.json for completion
   - Collects artifacts
   - Evaluates gates
   - Requests human approval

8. **Approve and merge**
   ```bash
   # Review the branch in your git host
   python -m orchestrator.main approve --task TASK-001
   # Then manually merge the PR / branch into main
   ```

9. **Check audit log**
   ```bash
   python -m orchestrator.main logs --task TASK-001
   python -m orchestrator.main status
   ```

---

## Phase 2: Planner → Executor Pipeline

**Goal**: Two-agent workflow where Planner produces a plan that Executor consumes.

### What to add

1. Bootstrap planner VM
2. Create a planning task (`agent_role: planner`)
3. Run planner: produces `plan.md`, `tasks.yaml`, `handoff.json`
4. Collect planner artifacts on Orchestrator
5. Distribute planner artifacts to executor workspace
6. Run executor task with planner plan as input
7. Full end-to-end Planner → Executor pipeline

### Implementation steps

```python
# Add to orchestrator/main.py as a new `pipeline` command:

@cli.command()
@click.option("--plan-task", required=True)
@click.option("--exec-task", required=True)
def pipeline(plan_task, exec_task):
    """Run a planner → executor pipeline."""
    # 1. Run planner task
    # 2. Collect planner artifacts
    # 3. Distribute to executor workspace
    # 4. Run executor task
```

---

## Phase 3: Debugger Agent

**Goal**: Automatic retry loop — Executor fails → Debugger analyzes → Executor retries.

### What to add

1. `agents/debugger/instructions.md` (already scaffolded)
2. Register a debugger worker VM
3. In `TaskManager.handle_failure()`: route to debugger before retry
4. Debugger produces `debug-report.md` + `fix-plan.yaml`
5. Executor receives debug artifacts and retries

---

## Phase 4: Reviewer Agent + Auto-Review Gate

**Goal**: Automated code review before human approval.

### What to add

1. `agents/reviewer/instructions.md` (already scaffolded)
2. Set `review_pass.required: true` in `config/factory.yaml`
3. Register reviewer worker VM
4. Reviewer runs after executor, produces `review-report.md`
5. `GateManager._gate_review_pass()` checks `REVIEW_PASSED` in report (already implemented)

---

## Phase 5: Observability Dashboard

**Goal**: Web UI showing factory state in real time.

### What to add

1. Small Flask/FastAPI server exposing state from SQLite
2. Worker status, task pipeline, gate results, artifact list
3. The JSONL audit log (`logs/factory.jsonl`) is already machine-readable

---

## Phase 6: Scheduling and Dependency Graphs

**Goal**: Declare multi-task pipelines with dependencies; Orchestrator schedules automatically.

### What to add

1. Pipeline definition file (DAG of tasks with `depends_on`)
2. `PipelineManager` that reads the DAG and schedules tasks as their deps complete
3. `StateManager` already has the event primitives to support this

---

## Known Limitations (Phase 1)

| Limitation | Mitigation |
|---|---|
| Bob non-interactive mode not confirmed | Structured context + task def as primary autonomy mechanism |
| No web UI | Use CLI `status` / `logs` commands |
| Single Orchestrator instance | Fine for Phase 1; add HA later if needed |
| SQLite not suitable for high concurrency | Swap to PostgreSQL in `StateManager._conn()` |
| Artifact store is local filesystem | Move to S3/object store for multi-host Orchestrator |
| No automatic PR creation | Add GitHub/GitLab API call after `approve` |
