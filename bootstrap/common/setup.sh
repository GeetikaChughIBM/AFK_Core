#!/usr/bin/env bash
# =============================================================================
# Software Factory — Common VM Setup
#
# Run on every worker VM before any role-specific bootstrap.
# Creates the factory workspace under ~/software-factory.
#
# Usage: ./setup.sh
# =============================================================================

set -euo pipefail

log() {
    echo "[SF-SETUP $(date '+%Y-%m-%dT%H:%M:%S')] $*"
}

log "SF_SETUP_STARTED on $(hostname) as $(whoami)"

# ── 1. Install base packages (sudo only for yum, not for directories) ──────────
log "Installing base packages"
sudo yum install -y \
    tmux \
    git \
    curl \
    wget \
    python3 \
    python3-pip \
    jq \
    unzip \
    2>&1 || true

# ── 2. Create factory workspace directories under home (no sudo needed) ────────
log "Creating workspace directory structure under ~/software-factory"
mkdir -p ~/software-factory/workspace
mkdir -p ~/software-factory/runtime/jobs
mkdir -p ~/software-factory/artifacts
mkdir -p ~/software-factory/logs
mkdir -p ~/software-factory/bootstrap/common

# ── 3. Write runtime marker ────────────────────────────────────────────────────
cat > ~/software-factory/runtime/worker.json << EOF
{
  "setup_version": "1.0.0",
  "setup_completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "username": "$(whoami)",
  "os": "$(uname -rs)"
}
EOF

log "Worker runtime marker written"

# ── 4. Configure git global identity placeholder ──────────────────────────────
# The Orchestrator overwrites these with the per-worker configured identity
# during task assignment. This is just a safe default.
git config --global user.name  "Software Factory Worker"
git config --global user.email "sf-worker@example.com"

log "SF_SETUP_SUCCESS"
log "Common setup complete. Workspace ready at ~/software-factory/"
