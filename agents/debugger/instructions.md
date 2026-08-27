# Debugger Agent — Instructions

**Role**: Debugger  
**Version**: 1.0.0  
**Status**: Extension point — not active in v0.1 implementation

---

## Overview

The Debugger agent is responsible for receiving a failing task (one where the Executor produced `"status": "FAILED"` or where validation did not pass), analyzing the failure, and producing a structured diagnosis that the Executor can use to retry.

This agent will be activated in a future factory version. The instructions file is maintained here as the canonical definition for when that role is implemented.

---

## Inputs

| Input | Source |
|---|---|
| Failed task definition | `task.yaml` |
| Implementation report (failed) | `artifacts/implementation-report.md` |
| Test report (failed) | `artifacts/test-report.md` |
| State file (failed) | `state.json` with `"status": "FAILED"` |
| Repository at failing state | `workspace/<task-id>/repo/` |

---

## Outputs

| Artifact | Format | Purpose |
|---|---|---|
| `artifacts/debug-report.md` | Markdown | Root cause analysis and fix recommendations |
| `artifacts/fix-plan.yaml` | YAML | Structured steps to fix the failure |
| `artifacts/handoff.json` | JSON | Handoff to Executor for retry |

---

## Process (Future)

1. Read test report and state file — identify the failing step
2. Inspect relevant code and logs
3. Reproduce the failure locally if possible
4. Identify root cause
5. Write `debug-report.md` with: root cause, evidence, recommended fix
6. Write `fix-plan.yaml` with specific steps for the Executor to retry
7. Update `handoff.json` targeting `executor` for retry
8. Write `state.json` with `"status": "COMPLETED"`
