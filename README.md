# Agentic Software Factory

A modular, observable, auditable, and extensible automated software development factory built on Fyre VMs, SSH orchestration, Bob agents, and structured artifact-based inter-agent communication.

---

## Architecture Overview

```
                    ORCHESTRATOR VM
                    (orchestrator.py)
                           │
              ┌────────────┼────────────┐
              │            │            │
             SSH          SSH          SSH
              │            │            │
              ▼            ▼            ▼
         Worker VM 1   Worker VM 2   Worker VM 3+
         (Planner)     (Executor)    (future roles)
              │            │            │
             Bob          Bob          Bob
              │            │            │
              └────────────┼────────────┘
                           │
                     Git Repository
```

---

## Project Structure

```
software-factory/
├── README.md
├── requirements.txt
│
├── config/
│   ├── factory.yaml              # Global factory settings
│   ├── workers.yaml              # Worker registry (no secrets)
│   └── secrets.env.example       # Secret injection template
│
├── bootstrap/
│   ├── db2/
│   │   ├── bld.md                # Human-readable build instructions
│   │   ├── commands.sh           # Deterministic build script
│   │   └── config.yaml           # Build configuration
│   └── common/
│       └── setup.sh              # Common VM preparation
│
├── agents/
│   ├── planner/
│   │   └── instructions.md
│   ├── executor/
│   │   └── instructions.md
│   ├── debugger/
│   │   └── instructions.md       # Extension point
│   └── reviewer/
│       └── instructions.md       # Extension point
│
├── schema/
│   ├── task.yaml                 # Task definition schema + example
│   ├── artifact.yaml             # Artifact envelope schema
│   └── handoff.yaml              # Handoff contract schema
│
├── orchestrator/
│   ├── __init__.py
│   ├── main.py                   # CLI entrypoint
│   ├── state.py                  # SQLite state manager
│   ├── worker_manager.py         # VM/SSH worker lifecycle
│   ├── bootstrap_manager.py      # VM provisioning/build
│   ├── task_manager.py           # Task assignment and tracking
│   ├── artifact_manager.py       # Artifact collection/distribution
│   ├── git_manager.py            # Branch/commit/push/merge
│   ├── gate_manager.py           # Automated gates + human approval
│   ├── ssh_client.py             # SSH abstraction
│   └── observability.py          # Structured logging
│
├── artifacts/                    # Shared artifact store (synced via SCP)
│   └── .gitkeep
│
├── db/
│   └── factory.db                # SQLite state database (auto-created)
│
└── logs/
    └── .gitkeep
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp config/secrets.env.example config/secrets.env
# Edit secrets.env — fill in SSH passwords/keys and Bob API keys
```

### 3. Configure workers

Edit `config/workers.yaml` — register your Fyre VMs (no secrets here).

### 4. Initialize the factory database

```bash
python -m orchestrator.main init
```

### 5. Provision and bootstrap a worker

```bash
python -m orchestrator.main bootstrap --worker worker-01
```

### 6. Assign a task

```bash
python -m orchestrator.main assign --task tasks/example-task.yaml --worker worker-01
```

### 7. Monitor state

```bash
python -m orchestrator.main status
python -m orchestrator.main status --worker worker-01
python -m orchestrator.main status --task TASK-001
```

### 8. Approve and merge

```bash
python -m orchestrator.main approve --task TASK-001
python -m orchestrator.main merge --task TASK-001
```

---

## State Model

### VM / Worker States

```
REGISTERED → PROVISIONING → BOOTSTRAPPING → READY → ASSIGNED → EXECUTING → AVAILABLE
                                 ↓                              ↓
                              FAILED                         FAILED
```

### Task States

```
QUEUED → ASSIGNED → PROVISIONING → EXECUTING → VALIDATING → AWAITING_HUMAN_REVIEW
                                      ↓              ↓                ↓
                                   FAILED         FAILED        REJECTED
                                                               APPROVED → MERGED
```

### Gate Sequence

```
PROVISIONING → BUILD_SUCCESS → ENVIRONMENT_READY → TASK_COMPLETE →
TESTS_PASS → REVIEW_PASS → HUMAN_APPROVAL → MERGED
```

---

## Security Model

- Secrets are **never** stored in `workers.yaml` or any tracked config file
- Credentials are injected at runtime via `config/secrets.env` (git-ignored)
- Worker Bob API keys are injected as environment variables over SSH
- Git identity is configured per-worker, not shared
- Workers operate in isolated `~/software-factory/workspace/<task-id>/` directories
- Main branch is **protected** — only the Orchestrator + human approval can merge
- Workers push to `factory/<task-id>` branches only

---

## Extension Points

| Component | How to extend |
|---|---|
| New agent role | Add `agents/<role>/instructions.md`, register worker with new role in `workers.yaml` |
| New project/repo | Add new `repository:` block to task definition |
| New bootstrap environment | Add `bootstrap/<env>/commands.sh` |
| New gate | Add gate function to `gate_manager.py` |
| New artifact type | Add schema to `schema/artifact.yaml` |
| Replace SQLite | Swap `state.py` backend — interface stays the same |
| Replace SSH | Swap `ssh_client.py` — interface stays the same |

---

## Initial VMs

| Role | Hostname | User |
|---|---|---|
| Orchestrator | `<your-orchestrator-vm>.dev.fyre.ibm.com` | `<username>` |
| Executor | `<your-executor-vm>.dev.fyre.ibm.com` | `<username>` |
| Planner | `<your-planner-vm>.dev.fyre.ibm.com` | `<username>` |
