"""The SQLite store: one file, one writer, one lock.

The default, and the reason the core needs nothing installed beside itself.
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
from api.models import schema

DIALECT = "sqlite"

#: SQLite has no row-level locking: concurrent writers are serialised by the
#: process lock below, so neither a claim nor a read-then-write needs a clause
#: of its own.
CLAIM_LOCK = ""
ROW_LOCK = ""

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
        conn.executescript(schema.tables(DIALECT))
        _migrate(conn)
        conn.executescript(schema.INDEXES)
        _conn, _conn_path = conn, path
        return conn


def _migrate(conn: sqlite3.Connection) -> None:
    present = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for column, kind in schema.later_columns(DIALECT).items():
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


def ping() -> None:
    """Cheapest possible proof that the store answers. Raises if it does not."""
    with reading() as conn:
        conn.execute("SELECT 1").fetchone()


def wipe() -> None:
    """Empty every table. For tests; the running service never calls it."""
    with transaction() as conn:
        for table in schema.TABLE_NAMES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
