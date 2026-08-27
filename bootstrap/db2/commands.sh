#!/usr/bin/env bash
# =============================================================================
# Software Factory — DB2 Build Bootstrap Script
#
# This is the CANONICAL deterministic build script for preparing a Fyre VM
# to the "ready for development" state for the DB2 project.
#
# Usage: ./commands.sh [--dry-run]
#
# The Orchestrator runs this via job-wrapper.sh inside a job-scoped tmux session:
#   tmux send-keys -t sf-build-{JOB_ID} 'bash job-wrapper.sh {JOB_ID} commands.sh' Enter
#
# Exit codes:
#   0 = success (all steps completed)
#   1 = step failure
#
# The Orchestrator detects completion by polling:
#   ~/software-factory/runtime/jobs/{JOB_ID}/status  (SUCCESS | FAILED | RUNNING)
#
# DO NOT rename or move this file without updating config.yaml and bld.md.
# =============================================================================

# Note: intentionally NOT using set -e here.
# setbldtree and git checkout emit warnings and may exit non-zero even on
# success (DB2 repo hooks do this). We check exit codes explicitly instead.

# Source the user profile explicitly — bash -l should cover this, but some
# systems only define build aliases in .bashrc which login shells skip.
# shellcheck source=/dev/null
for _f in ~/.bash_profile ~/.bashrc; do
    [[ -f "$_f" ]] && source "$_f"
done
unset _f

# SUPPTOOLS is required by setbldtree and bld (used in @INC for Perl modules).
# Set it here as a safety net in case the profile doesn't define it.
export SUPPTOOLS="${SUPPTOOLS:-/supp/tools}"
export PERL5LIB="${SUPPTOOLS}/perllib${PERL5LIB:+:$PERL5LIB}"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "[SF] DRY RUN mode — commands will be echoed but not executed"
fi

run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        echo "[SF] Running: $*"
        eval "$@"
    fi
}

run_allow_fail() {
    # Like run() but does not abort on non-zero exit.
    # Use for commands that emit warnings and exit non-zero even when OK.
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        echo "[SF] Running (allow-fail): $*"
        eval "$@" || true
    fi
}

log() {
    echo "[SF $(date '+%Y-%m-%dT%H:%M:%S')] $*"
}

set_step() {
    # Record the current build step so the Orchestrator can report progress.
    # Writes to $SF_JOB_DIR/current_step when running under job-wrapper.sh.
    local step="$1"
    log "CURRENT_STEP=$step"
    if [[ -n "${SF_JOB_DIR:-}" ]]; then
        echo "$step" > "$SF_JOB_DIR/current_step"
    fi
}

# ── Step 0: Emit start marker ──────────────────────────────────────────────────
log "SF_BUILD_STARTED"
log "Bootstrap script version: 1.1.0"
log "Host: $(hostname)"
log "User: $(whoami)"
log "Working directory: $(pwd)"

# ── Step 1: Install system prerequisites ──────────────────────────────────────
set_step "INSTALLING_PREREQUISITES"
log "STEP 1: Installing system prerequisites"
run_allow_fail "sudo yum install -y tmux git make gcc 2>&1"
log "STEP 1 COMPLETE"

# ── Step 2: Verify source directory exists ────────────────────────────────────
set_step "CHECKING_SOURCE_DIR"
log "STEP 2: Verifying DB2 source directory"
DB2_DIR="${DB2_SOURCE_DIR:-$HOME/db2}"

if [[ ! -d "$DB2_DIR" ]]; then
    log "ERROR: DB2 source directory not found at $DB2_DIR"
    log "DB2 source is expected to already exist on every Fyre VM at $DB2_DIR"
    log "SF_BUILD_FAILED"
    exit 1
fi

log "DB2 source directory found at: $DB2_DIR"
log "STEP 2 COMPLETE"

# ── Step 3: Navigate to source directory ──────────────────────────────────────
set_step "CHANGING_TO_SOURCE_DIR"
log "STEP 3: Changing to DB2 source directory"
cd "$DB2_DIR"
log "STEP 3 COMPLETE"

# ── Step 4: Switch to build branch ────────────────────────────────────────────
set_step "CHECKING_OUT_BRANCH"
log "STEP 4: Selecting build branch"
BUILD_BRANCH="${DB2_BUILD_BRANCH:-v1216}"

log "Current branch: $(git branch --show-current 2>/dev/null || echo unknown)"
log "Target branch:  $BUILD_BRANCH"

# allow-fail: git checkout in this repo triggers hooks that may exit non-zero
# even when the checkout itself succeeds (LFS healing, SUPPTOOLS sync, etc.)
run_allow_fail "git checkout $BUILD_BRANCH"

# Confirm we are actually on the right branch after the checkout
ACTUAL_BRANCH="$(git branch --show-current 2>/dev/null || echo unknown)"
log "Confirmed on branch: $ACTUAL_BRANCH"
if [[ "$ACTUAL_BRANCH" != "$BUILD_BRANCH" ]]; then
    log "ERROR: Expected branch $BUILD_BRANCH but got $ACTUAL_BRANCH"
    log "SF_BUILD_FAILED"
    exit 1
fi

# DB2's checkout hook warns to run git clean before building.
# Run it now so bld starts from a clean tree.
log "Cleaning repository before build (git clean -xdf)"
run_allow_fail "git clean -xdf"

log "STEP 4 COMPLETE — on branch: $BUILD_BRANCH"

# ── Step 5: Set build tree ─────────────────────────────────────────────────────
set_step "SET_BUILD_TREE"
log "STEP 5: Setting build tree"

# Verify setbldtree is available before calling it
if ! type setbldtree &>/dev/null; then
    log "ERROR: setbldtree not found in PATH — check that ~/.bashrc or ~/.bash_profile defines it"
    log "PATH=$PATH"
    log "SF_BUILD_FAILED"
    exit 1
fi

log "setbldtree type: $(type setbldtree)"

# setbldtree sets up build env files on disk. Run it and log any output.
# We are already in $DB2_DIR (cd'd in step 3), so ~/db2 works directly.
SETBLD_LOG="$(setbldtree ~/db2 2>&1)"
SETBLD_EXIT=$?
log "setbldtree output: ${SETBLD_LOG:-(none)}"
if [[ $SETBLD_EXIT -ne 0 ]]; then
    log "ERROR: setbldtree exited with code $SETBLD_EXIT"
    log "SF_BUILD_FAILED"
    exit 1
fi

# Explicitly add ~/db2/bin to PATH — bld lives there.
# setbldtree may not add it in non-interactive (non-login) shell contexts.
export PATH="$DB2_DIR/bin:$PATH"
log "PATH after setbldtree: $PATH"
log "bld location: $(which bld 2>&1)"

log "STEP 5 COMPLETE"

# ── Step 6: Run the build ──────────────────────────────────────────────────────
set_step "BUILDING"
BUILD_JOBS="${DB2_BUILD_JOBS:-16}"
log "STEP 6: Starting DB2 build with -j $BUILD_JOBS"
log "This step will take 1–3 hours on a standard Fyre VM."

bld -j "$BUILD_JOBS"
BUILD_EXIT=$?

if [[ $BUILD_EXIT -ne 0 ]]; then
    log "ERROR: bld exited with code $BUILD_EXIT"
    log "SF_BUILD_FAILED"
    exit 1
fi

log "STEP 6 COMPLETE — build exited with code 0"

# ── Final: Emit success sentinel ──────────────────────────────────────────────
# The Orchestrator polls for this exact string to detect build completion.
log "SF_BUILD_SUCCESS"
log "Worker VM is now in READY state. Bootstrap complete."
