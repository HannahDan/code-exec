"""SQLite schema and helpers."""

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DB_PATH = Path(os.getenv("CODE_EXEC_DB") or Path(__file__).parent.parent / "code_exec.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    tests TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    source TEXT NOT NULL CHECK(source IN ('llm','raw','edited')),
    code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    parent_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_name TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    test_results_json TEXT,
    duration_ms INTEGER,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    label TEXT NOT NULL CHECK(label IN ('correct','incorrect','partial')),
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    chosen_candidate_id TEXT NOT NULL,
    rejected_candidate_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid4().hex[:16]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript(SCHEMA)
    _migrate_candidates(conn)
    conn.close()


def _migrate_candidates(conn: sqlite3.Connection) -> None:
    """Pre-edit databases lack parent_id and reject source='edited'; SQLite can't alter a
    CHECK constraint, so the table is rebuilt with foreign keys off."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'candidates'").fetchone()[0]
    if "'edited'" in sql:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE candidates_new (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            source TEXT NOT NULL CHECK(source IN ('llm','raw','edited')),
            code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            parent_id TEXT
        );
        INSERT INTO candidates_new (id, task_id, source, code, created_at)
            SELECT id, task_id, source, code, created_at FROM candidates ORDER BY rowid;
        DROP TABLE candidates;
        ALTER TABLE candidates_new RENAME TO candidates;
        COMMIT;
        """
    )
    conn.execute("PRAGMA foreign_keys=ON")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


# ── Tasks ──────────────────────────────────────────────────────────

def insert_task(prompt: str, tests: str) -> dict:
    conn = _get_conn()
    task_id = _id()
    now = _now()
    conn.execute(
        "INSERT INTO tasks (id, prompt, tests, created_at) VALUES (?, ?, ?, ?)",
        (task_id, prompt, tests, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_task(task_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_task_tests(task_id: str, tests: str) -> None:
    conn = _get_conn()
    conn.execute("UPDATE tasks SET tests = ? WHERE id = ?", (tests, task_id))
    conn.commit()
    conn.close()


def delete_task(task_id: str) -> None:
    """Delete a task and everything hanging off it, in one transaction."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM preferences WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM annotations WHERE task_id = ?", (task_id,))
        conn.execute(
            "DELETE FROM runs WHERE candidate_id IN (SELECT id FROM candidates WHERE task_id = ?)",
            (task_id,),
        )
        conn.execute("DELETE FROM candidates WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.close()


_LATEST_RUNS = (
    "SELECT r.* FROM runs r WHERE r.rowid = "
    "(SELECT MAX(r2.rowid) FROM runs r2 WHERE r2.candidate_id = r.candidate_id)"
)


def list_tasks(exclude_prompt: str | None = None) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT t.*, "
        "COUNT(c.id) AS candidate_count, "
        "SUM(CASE WHEN lr.status = 'passed' THEN 1 ELSE 0 END) AS passed_count, "
        "SUM(CASE WHEN lr.status IN ('failed_tests','error','timeout','oom','infra_error') "
        "    THEN 1 ELSE 0 END) AS failed_count, "
        "SUM(CASE WHEN lr.status IN ('queued','running') THEN 1 ELSE 0 END) AS pending_count, "
        "(SELECT COUNT(*) FROM annotations a WHERE a.task_id = t.id) AS annotation_count "
        "FROM tasks t "
        "LEFT JOIN candidates c ON c.task_id = t.id "
        f"LEFT JOIN ({_LATEST_RUNS}) lr ON lr.candidate_id = c.id "
        "WHERE (? IS NULL OR t.prompt != ?) "
        "GROUP BY t.id ORDER BY t.created_at DESC",
        (exclude_prompt, exclude_prompt),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        for key in ("passed_count", "failed_count", "pending_count"):
            d[key] = d[key] or 0
        out.append(d)
    return out


# ── Candidates ─────────────────────────────────────────────────────

def insert_candidate(task_id: str, source: str, code: str, parent_id: str | None = None) -> dict:
    conn = _get_conn()
    cand_id = _id()
    now = _now()
    conn.execute(
        "INSERT INTO candidates (id, task_id, source, code, created_at, parent_id) VALUES (?, ?, ?, ?, ?, ?)",
        (cand_id, task_id, source, code, now, parent_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (cand_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_candidate(candidate_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_candidates_for_task(task_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM candidates WHERE task_id = ? ORDER BY rowid", (task_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── Runs ───────────────────────────────────────────────────────────

def insert_run(candidate_id: str, job_name: str | None = None) -> dict:
    conn = _get_conn()
    run_id = _id()
    now = _now()
    conn.execute(
        "INSERT INTO runs (id, candidate_id, job_name, status, started_at) VALUES (?, ?, ?, 'queued', ?)",
        (run_id, candidate_id, job_name, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_run(run_id: str, **kwargs) -> dict:
    conn = _get_conn()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [run_id]
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_run(run_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_latest_run_for_candidate(candidate_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM runs WHERE candidate_id = ? ORDER BY rowid DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_queued_or_running_runs() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT r.*, c.code, c.task_id, t.tests FROM runs r "
        "JOIN candidates c ON c.id = r.candidate_id "
        "JOIN tasks t ON t.id = c.task_id "
        "WHERE r.status IN ('queued', 'running') ORDER BY r.rowid"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_tests_for_run(run_id: str) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT t.tests FROM runs r "
        "JOIN candidates c ON c.id = r.candidate_id "
        "JOIN tasks t ON t.id = c.task_id "
        "WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    if row:
        return row["tests"]
    return None


# ── Annotations ────────────────────────────────────────────────────

def insert_annotation(task_id: str, candidate_id: str, label: str, notes: str | None = None) -> dict:
    conn = _get_conn()
    ann_id = _id()
    now = _now()
    conn.execute(
        "INSERT INTO annotations (id, task_id, candidate_id, label, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (ann_id, task_id, candidate_id, label, notes, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_annotations_for_task(task_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM annotations WHERE task_id = ? ORDER BY created_at", (task_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── Preferences ────────────────────────────────────────────────────

def insert_preference(task_id: str, chosen_candidate_id: str, rejected_candidate_id: str) -> dict:
    conn = _get_conn()
    pref_id = _id()
    now = _now()
    conn.execute(
        "INSERT INTO preferences (id, task_id, chosen_candidate_id, rejected_candidate_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (pref_id, task_id, chosen_candidate_id, rejected_candidate_id, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM preferences WHERE id = ?", (pref_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_all_preferences() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT p.*, t.prompt, "
        "c1.code AS chosen_code, c2.code AS rejected_code "
        "FROM preferences p "
        "JOIN tasks t ON t.id = p.task_id "
        "JOIN candidates c1 ON c1.id = p.chosen_candidate_id "
        "JOIN candidates c2 ON c2.id = p.rejected_candidate_id "
        "ORDER BY p.rowid"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_preferences_for_task(task_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM preferences WHERE task_id = ? ORDER BY rowid", (task_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── Async wrappers ─────────────────────────────────────────────────

async def async_call(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
