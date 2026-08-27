# Reviewer Agent — Instructions

**Role**: Reviewer  
**Version**: 1.0.0  
**Status**: Extension point — not active in v0.1 implementation

---

## Overview

The Reviewer agent performs automated code review on a completed Executor branch before it reaches the human approval gate. It checks for correctness, style, test coverage, and adherence to the plan.

This agent will be activated in a future factory version. The `review_pass` gate in `factory.yaml` is currently set to `required: false`.

---

## Inputs

| Input | Source |
|---|---|
| Task definition | `task.yaml` |
| Implementation plan | `artifacts/plan.md` |
| Implementation report | `artifacts/implementation-report.md` |
| Test report | `artifacts/test-report.md` |
| Change summary | `artifacts/change-summary.json` |
| Repository diff | `git diff main...factory/<task-id>` |

---

## Outputs

| Artifact | Format | Purpose |
|---|---|---|
| `artifacts/review-report.md` | Markdown | Review findings (pass/fail/comments) |
| `artifacts/handoff.json` | JSON | Handoff to human approval gate |

---

## Review Criteria (Future)

1. **Plan adherence** — do the changes match what `plan.md` specified?
2. **Correctness** — are there obvious logic errors?
3. **Test coverage** — do new code paths have tests?
4. **Style** — does the code follow existing project conventions?
5. **Scope** — are there changes not described in the task? Flag as out-of-scope.
6. **Security** — are credentials, secrets, or unsafe patterns introduced?

---

## Gate Outcome

- `REVIEW_PASSED` → handoff to human approval
- `REVIEW_FAILED` → handoff back to Executor or Debugger with findings
