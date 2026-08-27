# ARCHITECTURE.md — Software Factory Architecture Reference

## Component Map

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR VM                          │
│                                                             │
│  orchestrator/main.py  (CLI — operator interface)           │
│         │                                                   │
│         ├── WorkerManager     (VM registry + SSH)           │
│         │      └── SSHClient  (paramiko transport)          │
│         │                                                   │
│         ├── BootstrapManager  (provision + build monitor)   │
│         │                                                   │
│         ├── TaskManager       (assignment + workspace)      │
│         │                                                   │
│         ├── ArtifactManager   (collect + distribute)        │
│         │                                                   │
│         ├── GitManager        (clone + branch + verify)     │
│         │                                                   │
│         ├── GateManager       (automated gates + approval)  │
│         │                                                   │
│         └── StateManager      (SQLite state + audit log)    │
│                └── events table (append-only audit trail)   │
│                                                             │
│  config/factory.yaml     (factory settings)                 │
│  config/workers.yaml     (worker registry, no secrets)      │
│  config/secrets.env      (secrets — not committed)          │
│                                                             │
│  db/factory.db            (SQLite state database)           │
│  artifacts/<task-id>/     (collected artifacts)             │
│  logs/factory.jsonl       (structured audit log)            │
└─────────────────────────────────────────────────────────────┘
           │                              │
          SSH                            SSH
           │                              │
┌──────────▼──────────┐       ┌──────────▼──────────┐
│   WORKER VM         │       │   WORKER VM         │
│   (Planner)         │       │   (Executor)        │
│                     │       │                     │
│  Bob agent          │       │  Bob agent          │
│  BOB_SHELL_API_KEY  │       │  BOB_SHELL_API_KEY  │
│                     │       │                     │
│  /opt/sf/workspace/ │       │  /opt/sf/workspace/ │
│    <task-id>/       │       │    <task-id>/       │
│      repo/          │       │      repo/          │
│      artifacts/     │       │      artifacts/     │
│      task.json      │       │      task.json      │
│      instructions.md│       │      instructions.md│
│      state.json     │       │      state.json     │
│      logs/          │       │      logs/          │
└─────────────────────┘       └─────────────────────┘
           │                              │
           └──────────────┬───────────────┘
                          │
                    Git Repository
                   (protected main)
                   factory/<task-id> branches
```

---

## State Machines

### Worker State Machine

```
REGISTERED
    │
    ▼ bootstrap_worker()
PROVISIONING
    │
    ▼ common setup + script upload
BOOTSTRAPPING
    │
    ├─ success sentinel detected
    ▼
READY  ◄──────────────────────────────────────────┐
    │                                              │
    ▼ assign_task()                                │
ASSIGNED                                          │
    │                                              │
    ▼ run task pipeline                            │
EXECUTING                                         │
    │                                              │
    ├─ task completed / failed                     │
    ▼                                              │
AVAILABLE (→ READY) ──────────────────────────────┘
    │
    ├─ SSH unreachable
    ▼
OFFLINE
    │
    ├─ reconnects
    ▼
READY

    ├─ build/bootstrap fails
    ▼
FAILED
```

### Task State Machine

```
QUEUED
   │
   ▼ assign_task()
ASSIGNED
   │
   ▼ prepare_workspace() + setup_repo()
PROVISIONING
   │
   ▼ _launch_bob()
EXECUTING
   │
   ├─ poll_task_completion() → success
   ▼
VALIDATING
   │
   ├─ evaluate_all_gates() → all pass
   ▼
AWAITING_HUMAN_REVIEW
   │
   ├─ approve_task()          ├─ reject_task()
   ▼                          ▼
APPROVED                  REJECTED
   │
   ▼ (human git merge)
MERGED

   ┌─ any step fails
   ▼
FAILED
   │
   ├─ retry_count < max_retries
   ▼
QUEUED (retry)
   │
   ├─ retry_count >= max_retries
   ▼
FAILED (terminal)
```

---

## Gate Sequence

```
ENVIRONMENT_READY (worker bootstrap gate — not in task gates)
       │
       ▼
   TASK_COMPLETE       ← agent state.json status=COMPLETED, validation_passed=true
       │
       ▼
   TESTS_PASS          ← test-report.md has no FAIL entries
       │
       ▼
   BRANCH_PUSHED       ← change-summary.json has non-empty commit_sha
       │
       ▼
REQUIRED_ARTIFACTS     ← all outputs listed in task.yaml exist and are non-empty
       │
       ▼
  REVIEW_PASS          ← (optional, requires reviewer agent — off by default)
       │
       ▼
HUMAN_APPROVAL         ← operator runs `python -m orchestrator.main approve --task <id>`
       │
       ▼
    MERGED             ← human merges PR / branch into main
```

---

## Artifact Flow

```
Planner agent
    │
    ├── plan.md
    ├── tasks.yaml
    └── handoff.json  (status=READY_FOR_HANDOFF, consuming_agent=executor)
          │
          │ ArtifactManager.collect_artifacts()
          │ ArtifactManager.distribute_artifacts()
          ▼
Executor agent receives plan.md + tasks.yaml
    │
    ├── implementation-report.md
    ├── test-report.md
    ├── change-summary.json
    └── handoff.json  (status=READY_FOR_HANDOFF, consuming_agent=reviewer|orchestrator)
          │
          │ ArtifactManager.collect_artifacts()
          ▼
Orchestrator gate evaluation
    │
    └── GateManager reads local artifacts from artifacts/<task-id>/
```

---

## Bob Execution Model

Bob runs on each worker VM as an autonomous agent.
The factory's automation approach:

1. **Structured context** — Bob receives `instructions.md` (role-specific) and `task.json`
2. **Scoped API key** — `BOB_SHELL_API_KEY` env var, one per worker, injected over SSH
3. **Deterministic inputs** — task definition, plan, repo are all pre-placed in workspace
4. **State file contract** — Bob writes `state.json` when done; Orchestrator polls it
5. **Protected boundary** — Bob cannot push to main; workers only have access to `factory/` branches

**Future non-interactive mode**: when Bob exposes `--headless` or `--non-interactive`,
add it to `_launch_bob()` in `orchestrator/main.py`. The infrastructure is already in place.

---

## Extension Points

| To add... | Change |
|---|---|
| New agent role (Debugger) | Add `agents/debugger/instructions.md`, add worker in `workers.yaml` |
| New project | New `bootstrap/<project>/` directory + `commands.sh` |
| New gate | Add gate function to `GateManager`, add entry to `gates_to_run` list |
| New artifact type | Add to `schema/artifact.yaml` |
| SSH key auth instead of password | Update `WorkerManager`, change `workers.yaml` ref field name |
| Parallel task execution | `TaskManager.assign_task()` already supports multi-worker; add scheduling logic |
| PostgreSQL instead of SQLite | Swap `StateManager._conn()` backend |
| Vault secrets | Add `SecretProvider` ABC; swap `WorkerManager._load_secrets()` implementation |
| CI/CD trigger | Call `python -m orchestrator.main assign` from CI pipeline |
