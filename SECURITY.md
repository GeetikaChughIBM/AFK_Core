# SECURITY.md — Software Factory Security Model

## Secrets and Credentials

The Software Factory separates **configuration** (safe to commit) from **secrets** (never committed).

### What goes where

| Item | File | Git-tracked? |
|---|---|---|
| VM hostnames, usernames, roles | `config/workers.yaml` | ✅ Yes |
| Secret reference names (`WORKER_*_SSH_PASSWORD`) | `config/workers.yaml` | ✅ Yes |
| Actual SSH passwords | `config/secrets.env` | ❌ No |
| Bob Shell API keys | `config/secrets.env` | ❌ No |
| Git tokens/PATs | `config/secrets.env` | ❌ No |
| Factory config (timeouts, paths) | `config/factory.yaml` | ✅ Yes |

### Credential injection model

1. `config/secrets.env` is loaded by the Orchestrator at startup via `python-dotenv`
2. Secret *references* (e.g., `WORKER_EXECUTOR_01_SSH_PASSWORD`) are stored in `workers.yaml`
3. `WorkerManager.get_ssh_password(worker_id)` resolves the reference → actual value
4. The Bob API key is injected into the worker environment as `BOB_SHELL_API_KEY` **over SSH**, never written to disk on the Orchestrator
5. SSH passwords are never stored in Python variables longer than the connection lifetime

### Bob API key model

Each worker has its **own scoped Bob Shell API key**. Keys are:
- Stored only in `config/secrets.env`
- Referenced by name in `workers.yaml`
- Injected into the worker's tmux session as `BOB_SHELL_API_KEY=...` at execution time
- Not shared across workers
- Not logged (the key value is never written to `logs/factory.jsonl`)

**Key assignment:**

| Worker | Secret Ref |
|---|---|
| worker-planner-01 | `WORKER_PLANNER_01_BOB_API_KEY` |
| worker-executor-01 | `WORKER_EXECUTOR_01_BOB_API_KEY` |

### SSH authentication

The factory supports both password and key-based SSH:

```yaml
# workers.yaml (no secrets here)
ssh_password_secret_ref: "WORKER_EXECUTOR_01_SSH_PASSWORD"
```

To migrate to SSH key auth:
1. Generate key: `ssh-keygen -t ed25519 -f ~/.ssh/sf_worker_executor01`
2. Copy to worker: `ssh-copy-id -i ~/.ssh/sf_worker_executor01 geetika@<host>`
3. Change `workers.yaml` to reference a `ssh_key_path_secret_ref` instead
4. Update `WorkerManager._load_secrets()` to resolve the key path

### Branch protection

Workers are **only allowed to push to `factory/<task-id>` branches**.

This is enforced in two places:
1. `GitManager._is_safe_branch()` validates the branch before creation
2. The Orchestrator never calls git merge on main — that is a human operation

The following branches are protected and may never be pushed to by the factory:
- `main`
- `master`
- `release`

Additional protected branches can be added in `config/factory.yaml`:
```yaml
security:
  protected_branches:
    - main
    - master
    - release
```

### Worker isolation

Each worker operates in:
```
~/software-factory/workspace/&lt;task-id&gt;/
```

- Workers cannot see each other's task directories
- The Orchestrator owns artifact collection and distribution
- Workers do not have network access to each other directly

### Bob automation / non-interactive model

Bob agents on worker VMs run autonomously. The factory's permission model:

1. Each worker has a **single scoped API key** — the minimum required for that role
2. Bob receives a **structured instructions.md** that defines exactly what to do
3. The task definition includes explicit `success_criteria` and `validation` commands
4. Workers **cannot merge** — they push only to `factory/` branches
5. The Orchestrator is the **approval boundary** — no code reaches main without it
6. Human approval is the **final gate** before any merge

**If Bob supports a non-interactive/headless flag in the future**: add it to the `bob_cmd` in `main.py`'s `_launch_bob()`. The current implementation uses structured context injection as the primary autonomy mechanism.

### Future: Secrets manager migration

When ready to move beyond `secrets.env`:

1. Add a `SecretProvider` abstract base class to `orchestrator/secrets.py`
2. Implement `EnvSecretProvider` (current) and `VaultSecretProvider` (future)
3. Pass the provider into `WorkerManager.__init__()`
4. No other code changes required — all secret resolution goes through `WorkerManager`
