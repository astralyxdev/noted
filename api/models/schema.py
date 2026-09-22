"""The schema, written once and rendered for the engine in use.

Two engines mean two dialects, and two copies of a schema drift — the columns
stay in step for a while and then quietly stop. So the tables are written once
with a few tokens (`{pk}`, `{ts}`) and each store renders them. Everything
outside those tokens is SQL both engines already agree on.
"""

from __future__ import annotations

import re

#: What differs between SQLite and PostgreSQL in the DDL, and nothing else.
DIALECTS = {
    "sqlite": {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "ts": "REAL"},
    "postgres": {"pk": "BIGSERIAL PRIMARY KEY", "ts": "DOUBLE PRECISION"},
}

TABLES = """
CREATE TABLE IF NOT EXISTS tasks (
    id            {pk},
    task          TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    project       TEXT,
    assignee_id   TEXT,
    created_by    TEXT,
    parent_id     BIGINT  REFERENCES tasks(id) ON DELETE SET NULL,
    key           TEXT    UNIQUE,
    result        TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER,
    retry_after   {ts},
    lease_expires {ts},
    session_id    TEXT,
    created_at    {ts}    NOT NULL,
    updated_at    {ts}    NOT NULL
);

-- Dependencies: a task is not handed out until every predecessor is done.
-- A cycle cannot be built by construction: links are set at creation time, and
-- nothing references a brand new task yet.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id       BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);

-- An agent as a principal rather than a string. The key is stored hashed: it
-- is shown once at issue time and kept nowhere else.
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT    PRIMARY KEY,
    key_hash    TEXT    NOT NULL UNIQUE,
    projects    TEXT,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  {ts}    NOT NULL,
    revoked_at  {ts}
);

-- A session is one instance of an agent. The same key can run twice, and
-- without sessions those two processes are indistinguishable. Ids are never
-- reused.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id         TEXT   PRIMARY KEY,
    agent_id   TEXT   NOT NULL,
    transport  TEXT,
    opened_at  {ts}   NOT NULL,
    renewed_at {ts}   NOT NULL,
    closed_at  {ts}
);

-- The transition journal: append-only. Current state lives in tasks; this is
-- how it got there, without which an incident cannot be reconstructed.
CREATE TABLE IF NOT EXISTS task_events (
    id          {pk},
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at          {ts}   NOT NULL,
    event       TEXT   NOT NULL,
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
    "retry_after": "{ts}",
    "lease_expires": "{ts}",
    "session_id": "TEXT",
}

#: Dropped in the order that respects the foreign keys. Tests wipe between
#: cases; nothing in the running service ever calls this.
TABLE_NAMES = ("task_events", "task_deps", "tasks", "agent_sessions", "agents")


def tables(dialect: str) -> str:
    return TABLES.format(**DIALECTS[dialect])


def later_columns(dialect: str) -> dict[str, str]:
    return {name: kind.format(**DIALECTS[dialect]) for name, kind in LATER_COLUMNS.items()}


#: Line comments are dropped before splitting: the prose above a table is for
#: whoever reads this file, and a semicolon inside a sentence would otherwise
#: cut a statement in half.
_COMMENT = re.compile(r"--[^\n]*")


def statements(script: str) -> list[str]:
    """The script split into statements. SQLite has executescript; psycopg
    would rather be handed one statement at a time."""
    bare = _COMMENT.sub("", script)
    return [part.strip() for part in bare.split(";") if part.strip()]
