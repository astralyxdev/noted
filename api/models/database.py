"""Соединение с SQLite и схема.

Писатель у базы ровно один — процесс ядра. Внутри процесса запросы всё равно
конкурентны, поэтому все операции идут через один RLock: это и есть явная
сериализация, обещанная в SPEC.md. WAL оставлен ради внешних читателей.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    project     TEXT,
    assignee_id TEXT,
    created_by  TEXT,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    key         TEXT    UNIQUE,
    result      TEXT,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
"""

#: Индексы создаются ПОСЛЕ миграции: на старой базе таблица уже есть, и индекс
#: по новой колонке упадёт, если досыпать её позже.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_queue   ON tasks(status, assignee_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status, id);
"""

#: Колонки, добавленные после первой версии схемы. База могла быть создана
#: раньше, поэтому CREATE TABLE IF NOT EXISTS их не добавит — досыпаем вручную.
LATER_COLUMNS = {"project": "TEXT"}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def db_path() -> Path:
    raw = os.environ.get("NOTED_DB")
    return Path(raw).expanduser() if raw else Path.home() / ".noted" / "tasks.db"


def connection() -> sqlite3.Connection:
    """Единственное соединение на процесс. Переоткрывается, если сменился NOTED_DB."""
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
    """Запись под локом. IMMEDIATE берёт write-лок сразу, поэтому
    read-then-write внутри остаётся атомарным."""
    with _lock:
        conn = connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
