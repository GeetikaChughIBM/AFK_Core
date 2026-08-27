#!/usr/bin/env bash
# =============================================================================
# Software Factory — Job Wrapper
#
# Wraps any build script with a file-based status protocol so the Orchestrator
# can poll ~/software-factory/runtime/jobs/$JOB_ID/status instead of trying
# to infer state from tmux pane output.
#
# Usage (called by the Orchestrator, not by hand):
#   bash job-wrapper.sh <JOB_ID> <script> [args...]
#
# All exported variables in the calling shell (DB2_SOURCE_DIR, etc.) are
# automatically inherited — do NOT use bash -l here; the tmux command that
# invokes this wrapper is already a login shell context.
#
# Files written under ~/software-factory/runtime/jobs/$JOB_ID/:
#   status        — RUNNING | SUCCESS | FAILED
#   pid           — PID of the build process
#   current_step  — last step name written by the build script
#   exit_code     — numeric exit code of the build script
#   started_at    — ISO-8601 UTC timestamp
#   finished_at   — ISO-8601 UTC timestamp
#   stdout.log    — combined stdout+stderr of the build script
# =============================================================================

set -uo pipefail
# Note: no set -e — we must capture the exit code ourselves

JOB_ID="${1:?JOB_ID required}"
shift
SCRIPT="${1:?script path required}"
shift  # any remaining args are forwarded to the build script

JOB_DIR="$HOME/software-factory/runtime/jobs/$JOB_ID"
mkdir -p "$JOB_DIR"

STATUS_FILE="$JOB_DIR/status"
PID_FILE="$JOB_DIR/pid"
STEP_FILE="$JOB_DIR/current_step"
EXIT_CODE_FILE="$JOB_DIR/exit_code"
STARTED_FILE="$JOB_DIR/started_at"
FINISHED_FILE="$JOB_DIR/finished_at"
LOG_FILE="$JOB_DIR/stdout.log"

_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Export SF_JOB_DIR so the build script can write current_step
export SF_JOB_DIR="$JOB_DIR"

# Initialise state files
echo "RUNNING"    > "$STATUS_FILE"
echo "STARTING"   > "$STEP_FILE"
echo "$(_ts)"     > "$STARTED_FILE"

echo "[wrapper] JOB_ID=$JOB_ID" | tee "$LOG_FILE"
echo "[wrapper] script=$SCRIPT  args=$*" | tee -a "$LOG_FILE"
echo "[wrapper] started_at=$(_ts)" | tee -a "$LOG_FILE"
echo "[wrapper] SF_JOB_DIR=$JOB_DIR" | tee -a "$LOG_FILE"

# Launch the build script, capturing its PID for the pid file.
# We run bash in the background, save its PID before attaching tee,
# then wait specifically for the bash process so EXIT_CODE reflects
# the build result — not tee's exit (which is always 0).
bash "$SCRIPT" "$@" > "$JOB_DIR/_build.stdout" 2>&1 &
BUILD_PID=$!
echo "$BUILD_PID" > "$PID_FILE"
echo "[wrapper] build PID=$BUILD_PID" | tee -a "$LOG_FILE"

# Tail the build output to the tmux pane AND the log file in real time.
tail -f "$JOB_DIR/_build.stdout" >> "$LOG_FILE" &
TAIL_PID=$!

# Wait for the build to finish and capture its true exit code.
wait "$BUILD_PID"
EXIT_CODE=$?

# Stop the tail follower
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true

# Append any final output that tail may have missed
cat "$JOB_DIR/_build.stdout" >> "$LOG_FILE" 2>/dev/null || true
rm -f "$JOB_DIR/_build.stdout"

echo "[wrapper] finished_at=$(_ts)  exit_code=$EXIT_CODE" | tee -a "$LOG_FILE"

echo "$EXIT_CODE" > "$EXIT_CODE_FILE"
echo "$(_ts)"     > "$FINISHED_FILE"

if [[ $EXIT_CODE -eq 0 ]]; then
    echo "DONE"    > "$STEP_FILE"
    echo "SUCCESS" > "$STATUS_FILE"
    echo "[wrapper] SF_JOB_SUCCESS"
else
    echo "FAILED"  > "$STEP_FILE"
    echo "FAILED"  > "$STATUS_FILE"
    echo "[wrapper] SF_JOB_FAILED exit_code=$EXIT_CODE"
fi

exit "$EXIT_CODE"
