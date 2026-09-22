"""The SQLite connection and the schema.

There is exactly one writer — the core process. Requests inside that process
are still concurrent, so every operation goes through a single RLock: that is
the explicit serialisation SPEC.md promises. WAL stays on for outside readers.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from api import settings

TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task          TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    project       TEXT,
    assignee_id   TEXT,
    created_by    TEXT,
    parent_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    key           TEXT    UNIQUE,
    result        TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER,
    retry_after   REAL,
    lease_expires REAL,
    session_id    TEXT,                         -- which session holds the task
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);

-- Dependencies: a task is not handed out until every predecessor is done.
-- A cycle cannot be built by construction: links are set at creation time, and
-- nothing references a brand new task yet.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);

-- An agent as a principal rather than a string. The key is stored hashed: it
-- is shown once at issue time and kept nowhere else.
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT    PRIMARY KEY,           -- the name, and the assignee_id
    key_hash    TEXT    NOT NULL UNIQUE,
    projects    TEXT,                          -- JSON list; NULL means any
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    revoked_at  REAL
);

-- A session is one instance of an agent. The same key can run twice, and
-- without sessions those two processes are indistinguishable. Ids are never
-- reused.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    transport  TEXT,                           -- http | stdio, for diagnostics
    opened_at  REAL NOT NULL,
    renewed_at REAL NOT NULL,
    closed_at  REAL
);

-- The transition journal: append-only. Current state lives in tasks; this is
-- how it got there, without which an incident cannot be reconstructed.
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at          REAL    NOT NULL,
    event       TEXT    NOT NULL,
    actor       TEXT,
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT
);
"""

#: Indexes are created AFTER the migration: on an older database the table
#: already exists, and an index over a new column fails if the column is added
#: afterwards.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_queue    ON tasks(status, assignee_id, priority, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project  ON tasks(project, status, id);
CREATE INDEX IF NOT EXISTS idx_tasks_lease    ON tasks(status, lease_expires);
CREATE INDEX IF NOT EXISTS idx_tasks_creator  ON tasks(created_by, created_at);
CREATE INDEX IF NOT EXISTS idx_deps_reverse   ON task_deps(depends_on_id);
CREATE INDEX IF NOT EXISTS idx_events_task    ON task_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_session  ON tasks(session_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_alive ON agent_sessions(closed_at, renewed_at);
"""

#: Columns added after the first version of the schema. A database may predate
#: them, and CREATE TABLE IF NOT EXISTS will not add them, so we do it by hand.
LATER_COLUMNS = {
    "project": "TEXT",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER",
    "retry_after": "REAL",
    "lease_expires": "REAL",
    "session_id": "TEXT",
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def db_path() -> Path:
    return settings.database_path()


def connection() -> sqlite3.Connection:
    """The single connection per process. Reopened when NOTED_DB changes."""
    global _conn, _conn_path
    with _lock:
        path = db_path()
        if _conn is not None and _conn_path == path:
            return _conn
        if _conn is not None:
            _conn.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(TABLE)
        _migrate(conn)
        conn.executescript(INDEXES)
        _conn, _conn_path = conn, path
        return conn


def _migrate(conn: sqlite3.Connection) -> None:
    present = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for column, kind in LATER_COLUMNS.items():
        if column not in present:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")


def close() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None


@contextmanager
def reading() -> Iterator[sqlite3.Connection]:
    with _lock:
        yield connection()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A write under the lock. IMMEDIATE takes the write lock up front, which
    keeps a read-then-write inside it atomic."""
    with _lock:
        conn = connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
