# DB2 Build Environment — Bootstrap Instructions

**Version**: 1.0.0  
**Target**: Fyre x86 VM (RHEL/CentOS)  
**Canonical build script**: `commands.sh`  
**Configuration**: `config.yaml`

---

## Overview

This document defines the canonical, human-readable instructions for preparing a Fyre VM to the **"ready for development"** state for the DB2 project. The Orchestrator reads `config.yaml` to determine parameters and executes `commands.sh` deterministically inside a `tmux` session so SSH disconnections cannot interrupt the build.

> **Rule**: If you need to change how a VM is prepared, update these files. The Orchestrator and every future worker derive their environment from this canonical definition — never from ad-hoc manual steps.

---

## Prerequisites

The Orchestrator will verify these conditions before starting the bootstrap:

| Check | Expected |
|---|---|
| OS | RHEL 7/8/9 or CentOS equivalent |
| CPU | ≥ 4 cores recommended for `bld -j 16` |
| RAM | ≥ 16 GB recommended |
| Disk | ≥ 50 GB free in `~` |
| Network | Access to internal IBM build infrastructure |
| `tmux` | Must be installed (`yum install -y tmux`) |
| `git` | Must be installed |
| `~/db2` | **DB2 source already exists on every Fyre VM** — the factory does not clone it. The factory only runs the build. |

---

## Build Steps

The steps below map directly to the commands in `commands.sh`. They are numbered so log output and state transitions can reference them.

### Step 1 — Install system prerequisites

Install `tmux`, `git`, and any other OS-level packages required before building.

```bash
sudo yum install -y tmux git make gcc
```

### Step 2 — Start tmux session

All subsequent commands run inside a named tmux session so that SSH disconnection does not kill the build.

```bash
tmux new-session -d -s sf-build-db2
```

The Orchestrator monitors this session by name. **Do not rename or kill this session manually during a factory-managed build.**

### Step 3 — Navigate to the DB2 source directory

```bash
cd ~/db2
```

This path is configured in `config.yaml` (`source_dir`). `~/db2` is expected to already exist on every Fyre VM — the factory does not clone it. The Orchestrator's job is only to run the build, not to provision the source.

### Step 4 — Select the correct branch

```bash
git branch
git checkout v1216
```

Branch name is configured in `config.yaml` (`build_branch`).

### Step 5 — Set the build tree

```bash
setbldtree ~/db2
```

Sets the IBM build environment to the source directory.

### Step 6 — Run the build

```bash
bld -j 16
```

`-j 16` runs 16 parallel jobs. This is configured in `config.yaml` (`build_parallelism`). Expect this to take **1–3 hours** on a standard Fyre VM.

---

## Success Criteria

The bootstrap is considered complete and the worker VM is marked **READY** when **all** of the following are true:

1. `bld -j 16` exits with return code `0`
2. The tmux session exits cleanly (or the Orchestrator detects the sentinel output)
3. The Orchestrator's gate check `BUILD_SUCCESS` passes
4. The `validate.sh` (in `common/`) passes if applicable

The Orchestrator polls `tmux capture-pane -t sf-build-db2` every 30 seconds (configurable) and scans for the sentinel string defined in `config.yaml` (`build_success_sentinel`).

---

## Failure Handling

| Situation | Orchestrator Action |
|---|---|
| `bld` exits non-zero | Mark VM `FAILED`, log full tmux output, retry up to `max_retries` |
| SSH disconnection mid-build | Reconnect, re-attach to tmux session, continue polling |
| tmux session not found | Mark VM `FAILED`, try to re-run from last known step |
| Timeout exceeded | Mark VM `FAILED`, capture current tmux output for diagnosis |
| Build produces known error pattern | Log as structured error, notify operator |

---

## Cleanup / Re-provisioning

To re-run a bootstrap from scratch on a VM:

```bash
python -m orchestrator.main bootstrap --worker <worker-id> --force-reset
```

This will:
1. Kill any existing `sf-build-*` tmux sessions
2. Reset the worker state to `REGISTERED`
3. Re-run the full bootstrap sequence

---

## Notes

- `~/db2` already exists on every Fyre VM. The factory does not clone or manage the DB2 source — it only runs the build.
- `commands.sh` uses `set -e` — any failing command aborts the script, which surfaces clearly in logs.
- The sentinel string approach is preferred over polling exit codes because `tmux send-keys` is fire-and-forget; the Orchestrator must detect completion from output, not from a process handle.
