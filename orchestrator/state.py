"""
state.py — SQLite-backed persistent state manager for the Software Factory.

All factory state lives here:
  - workers      (VMs, their lifecycle, current status)
  - tasks        (assignments, current state, retry count)
  - jobs         (individual tmux/process jobs on workers)
  - artifacts    (produced artifacts per task)
  - gates        (gate evaluation results per task)
  - events       (append-only audit log)

The interface is intentionally simple so the SQLite backend can be swapped
for PostgreSQL or another store without touching the rest of the Orchestrator.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from orchestrator.observability import get_logger

log = get_logger("state")

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    display_name    TEXT,
    host            TEXT NOT NULL,
    port            INTEGER DEFAULT 22,
    username        TEXT NOT NULL,
    role            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'registered',
    bootstrap_name  TEXT,
    git_user_name   TEXT,
    git_user_email  TEXT,
    capabilities    TEXT,    -- JSON array
    tags            TEXT,    -- JSON array
    ssh_password_secret_ref  TEXT,
    bob_api_key_secret_ref   TEXT,
    current_task_id TEXT,
    last_seen       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    metadata        TEXT     -- JSON blob for extension fields
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    agent_role      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    worker_id       TEXT REFERENCES workers(id),
    branch          TEXT,
    repo_url        TEXT,
    base_branch     TEXT,
    priority        TEXT DEFAULT 'normal',
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,
    timeout_seconds INTEGER DEFAULT 3600,
    definition      TEXT,    -- full task YAML as JSON string
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    assigned_at     TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    error_message   TEXT,
    metadata        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id),
    worker_id       TEXT REFERENCES workers(id),
    job_type        TEXT NOT NULL,   -- bootstrap | build | execute | validate
    tmux_session    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    completed_at    TEXT,
    exit_code       INTEGER,
    last_output     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id),
    artifact_type   TEXT NOT NULL,
    producing_agent TEXT,
    format          TEXT,
    path_on_worker  TEXT,
    path_local      TEXT,
    content_hash    TEXT,
    available       INTEGER DEFAULT 1,
    validated       INTEGER DEFAULT 0,
    produced_at     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id),
    gate_name       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|pass|fail|skipped
    required        INTEGER DEFAULT 1,
    evaluated_at    TEXT,
    details         TEXT,   -- JSON blob with gate evaluation details
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    task_id         TEXT,
    worker_id       TEXT,
    actor           TEXT,
    message         TEXT,
    payload         TEXT    -- JSON blob
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_worker   ON tasks(worker_id);
CREATE INDEX IF NOT EXISTS idx_jobs_task      ON jobs(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_gates_task     ON gates(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task    ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_worker  ON events(worker_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# ── StateManager ─────────────────────────────────────────────────────────────

class StateManager:
    """
    Thread-safe SQLite-backed state store for the Software Factory.
    All public methods take/return plain dicts — no ORM, no magic.
    """

    def __init__(self, db_path: str = "db/factory.db") -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
        log.info("State database initialised", db=self._db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Workers ───────────────────────────────────────────────────────────────

    def upsert_worker(self, worker: Dict[str, Any]) -> None:
        """Insert or update a worker record from workers.yaml data."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO workers
                    (id, display_name, host, port, username, role, status,
                     bootstrap_name, git_user_name, git_user_email,
                     capabilities, tags,
                     ssh_password_secret_ref, bob_api_key_secret_ref,
                     created_at, updated_at)
                VALUES
                    (:id,:display_name,:host,:port,:username,:role,:status,
                     :bootstrap_name,:git_user_name,:git_user_email,
                     :capabilities,:tags,
                     :ssh_password_secret_ref,:bob_api_key_secret_ref,
                     :created_at,:updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=excluded.display_name,
                    host=excluded.host,
                    port=excluded.port,
                    username=excluded.username,
                    role=excluded.role,
                    bootstrap_name=excluded.bootstrap_name,
                    git_user_name=excluded.git_user_name,
                    git_user_email=excluded.git_user_email,
                    capabilities=excluded.capabilities,
                    tags=excluded.tags,
                    ssh_password_secret_ref=excluded.ssh_password_secret_ref,
                    bob_api_key_secret_ref=excluded.bob_api_key_secret_ref,
                    updated_at=excluded.updated_at
                """,
                {
                    "id": worker["id"],
                    "display_name": worker.get("display_name", ""),
                    "host": worker["host"],
                    "port": worker.get("port", 22),
                    "username": worker["username"],
                    "role": worker["role"],
                    "status": worker.get("status", "registered"),
                    "bootstrap_name": worker.get("bootstrap"),
                    "git_user_name": worker.get("git", {}).get("user_name", ""),
                    "git_user_email": worker.get("git", {}).get("user_email", ""),
                    "capabilities": json.dumps(worker.get("capabilities", [])),
                    "tags": json.dumps(worker.get("tags", [])),
                    "ssh_password_secret_ref": worker.get("ssh_password_secret_ref"),
                    "bob_api_key_secret_ref": worker.get("bob_api_key_secret_ref"),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def set_worker_status(
        self, worker_id: str, status: str, task_id: Optional[str] = None
    ) -> None:
        valid = {
            "registered", "provisioning", "bootstrapping",
            "ready", "assigned", "executing", "failed", "offline",
        }
        if status not in valid:
            raise ValueError(f"Invalid worker status: {status!r}")

        with self._conn() as conn:
            conn.execute(
                "UPDATE workers SET status=?, current_task_id=?, updated_at=? WHERE id=?",
                (status, task_id, _now(), worker_id),
            )
        log.event(
            "WORKER_STATE_CHANGE", f"Worker {worker_id} → {status}",
            worker_id=worker_id, status=status, task_id=task_id,
        )
        self.add_event("WORKER_STATE_CHANGE", worker_id=worker_id,
                       message=f"status={status}", payload={"status": status, "task_id": task_id})

    def get_worker(self, worker_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_workers(self, role: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        sql = "SELECT * FROM workers WHERE 1=1"
        params: list = []
        if role:
            sql += " AND role=?"
            params.append(role)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY role, id"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(self, task_def: Dict) -> str:
        task_id = task_def.get("task_id") or _gen_id("TASK")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (id, title, agent_role, status, repo_url, base_branch,
                     priority, max_retries, timeout_seconds, definition,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    task_def.get("title", ""),
                    task_def.get("agent_role", "executor"),
                    "queued",
                    task_def.get("repository", {}).get("url", ""),
                    task_def.get("repository", {}).get("base_branch", "main"),
                    task_def.get("metadata", {}).get("priority", "normal"),
                    task_def.get("policy", {}).get("max_retries", 2),
                    task_def.get("policy", {}).get("timeout_seconds", 3600),
                    json.dumps(task_def),
                    now,
                    now,
                ),
            )
        log.event("TASK_CREATED", f"Task {task_id} created", task_id=task_id)
        return task_id

    def set_task_status(
        self,
        task_id: str,
        status: str,
        worker_id: Optional[str] = None,
        branch: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        valid = {
            "queued", "assigned", "provisioning", "executing",
            "validating", "failed", "waiting_for_input",
            "completed", "awaiting_human_review", "approved", "merged", "rejected",
        }
        if status not in valid:
            raise ValueError(f"Invalid task status: {status!r}")

        now = _now()
        updates: Dict[str, Any] = {"status": status, "updated_at": now}
        if worker_id:
            updates["worker_id"] = worker_id
        if branch:
            updates["branch"] = branch
        if error_message:
            updates["error_message"] = error_message
        if status == "assigned":
            updates["assigned_at"] = now
        if status == "executing":
            updates["started_at"] = now
        if status in ("completed", "merged", "rejected", "failed"):
            updates["completed_at"] = now

        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [task_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)

        log.event(
            "TASK_STATE_CHANGE", f"Task {task_id} → {status}",
            task_id=task_id, status=status, worker_id=worker_id,
        )
        self.add_event("TASK_STATE_CHANGE", task_id=task_id,
                       message=f"status={status}", payload={"status": status})

    def get_task(self, task_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status: Optional[str] = None, worker_id: Optional[str] = None) -> List[Dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if worker_id:
            sql += " AND worker_id=?"
            params.append(worker_id)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def increment_retry(self, task_id: str) -> int:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET retry_count=retry_count+1, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            row = conn.execute("SELECT retry_count FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row["retry_count"] if row else 0

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def create_job(
        self, task_id: Optional[str], worker_id: str, job_type: str, tmux_session: Optional[str] = None
    ) -> str:
        job_id = _gen_id("JOB")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, task_id, worker_id, job_type, tmux_session,
                                  status, created_at, updated_at)
                VALUES (?,?,?,?,?,'pending',?,?)
                """,
                (job_id, task_id, worker_id, job_type, tmux_session, now, now),
            )
        return job_id

    def set_job_status(
        self,
        job_id: str,
        status: str,
        exit_code: Optional[int] = None,
        last_output: Optional[str] = None,
    ) -> None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status=?, exit_code=?, last_output=?, updated_at=?,
                    started_at=CASE WHEN status='pending' AND ?='running' THEN ? ELSE started_at END,
                    completed_at=CASE WHEN ? IN ('success','failed','timeout') THEN ? ELSE completed_at END
                WHERE id=?
                """,
                (status, exit_code, last_output, now,
                 status, now,
                 status, now,
                 job_id),
            )

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_latest_job(
        self, worker_id: str, job_type: Optional[str] = None
    ) -> Optional[Dict]:
        """Return the most recently created job for a worker, optionally filtered by type."""
        sql = "SELECT * FROM jobs WHERE worker_id=?"
        params: list = [worker_id]
        if job_type:
            sql += " AND job_type=?"
            params.append(job_type)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def set_job_tmux_session(self, job_id: str, tmux_session: str) -> None:
        """Update the tmux_session field for an existing job."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET tmux_session=?, updated_at=? WHERE id=?",
                (tmux_session, _now(), job_id),
            )

    # ── Artifacts ─────────────────────────────────────────────────────────────

    def register_artifact(
        self,
        task_id: str,
        artifact_type: str,
        path_on_worker: str,
        producing_agent: str = "",
        fmt: str = "text",
        path_local: str = "",
        content_hash: str = "",
    ) -> str:
        art_id = _gen_id("ART")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, task_id, artifact_type, producing_agent, format,
                     path_on_worker, path_local, content_hash, produced_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (art_id, task_id, artifact_type, producing_agent, fmt,
                 path_on_worker, path_local, content_hash, now, now),
            )
        log.event("ARTIFACT_PRODUCED", f"Artifact {artifact_type} registered for task {task_id}",
                  task_id=task_id, artifact_type=artifact_type, artifact_id=art_id)
        return art_id

    def list_artifacts(self, task_id: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Gates ─────────────────────────────────────────────────────────────────

    def record_gate(
        self,
        task_id: str,
        gate_name: str,
        status: str,
        required: bool = True,
        details: Optional[Dict] = None,
    ) -> None:
        gate_id = _gen_id("GATE")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO gates (id, task_id, gate_name, status, required, evaluated_at, details, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING
                """,
                (gate_id, task_id, gate_name, status, int(required),
                 now, json.dumps(details or {}), now),
            )
        event_type = "GATE_PASS" if status == "pass" else "GATE_FAIL"
        log.event(event_type, f"Gate {gate_name} {status} for task {task_id}",
                  task_id=task_id, gate=gate_name, status=status)
        self.add_event(event_type, task_id=task_id,
                       message=f"gate={gate_name} status={status}",
                       payload={"gate": gate_name, "status": status, "details": details})

    def list_gates(self, task_id: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM gates WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Events (audit log) ────────────────────────────────────────────────────

    def add_event(
        self,
        event_type: str,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        actor: str = "orchestrator",
        message: str = "",
        payload: Optional[Dict] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (ts, event_type, task_id, worker_id, actor, message, payload)
                VALUES (?,?,?,?,?,?,?)
                """,
                (_now(), event_type, task_id, worker_id, actor, message,
                 json.dumps(payload or {})),
            )

    def list_events(
        self,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if worker_id:
            sql += " AND worker_id=?"
            params.append(worker_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]
