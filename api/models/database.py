"""Which store the core is talking to, and the four calls the services use.

SQLite by default: one file, nothing to install, and the whole service is one
container. Set `NOTED_DB_URL` and the same code runs on PostgreSQL instead —
same tables, same SQL, a pool and row-level locking underneath rather than a
single connection behind a process lock.

The services never learn which one they got. They ask for `reading()` or
`transaction()` and write one dialect of SQL; the difference between the
engines lives in the two store modules and in `claim_lock()`.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import ModuleType
from typing import Any, Iterator

from api import settings
from api.models import sqlite_store

#: A row is a mapping of column name to value under both engines: `sqlite3.Row`
#: on one side, a dict from psycopg's `dict_row` on the other.
Row = Any
Connection = Any


def _postgres() -> ModuleType:
    # Imported on demand: psycopg is optional, and an installation that stays
    # on SQLite should not need it present.
    from api.models import postgres_store

    return postgres_store


def store() -> ModuleType:
    return _postgres() if settings.database_url() else sqlite_store


def dialect() -> str:
    return store().DIALECT


def claim_lock() -> str:
    """The clause that makes a claim safe against a concurrent claimer.

    Empty on SQLite, where the process lock already serialises writers;
    `FOR UPDATE SKIP LOCKED` on PostgreSQL, where they run in parallel.
    """
    return store().CLAIM_LOCK


def row_lock(of: str | None = None) -> str:
    """The clause that holds one row still between reading it and writing it.

    Compare-and-set, ownership and fencing are all decided by reading a task
    and then updating it. On SQLite the process lock makes that pair atomic; on
    PostgreSQL two requests run side by side, and without `FOR UPDATE` both
    would pass the same check before either wrote.

    `of` names the table to lock when the query joins others: PostgreSQL will
    not lock the rows of a joined table it is only reading through.
    """
    clause = store().ROW_LOCK
    if clause and of:
        return f"{clause} OF {of}"
    return clause


@contextmanager
def reading() -> Iterator[Connection]:
    with store().reading() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Connection]:
    with store().transaction() as conn:
        yield conn


def close() -> None:
    """Drop every open handle. Both stores are closed, not just the current
    one: tests move between engines inside a single process."""
    sqlite_store.close()
    try:
        _postgres().close()
    except ImportError:
        pass


def ping() -> None:
    """Ask the store whether it is actually there. Raises if it is not."""
    store().ping()


def wipe() -> None:
    """Empty every table of the current store. For tests only."""
    store().wipe()
