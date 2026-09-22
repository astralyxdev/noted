"""The PostgreSQL store: a pool, real transactions, row-level locking.

Chosen by setting `NOTED_DB_URL`. What changes against SQLite is not the SQL —
the services write one dialect and it is rendered here — but the shape of the
concurrency:

* SQLite has one connection behind one process lock, so writers queue up;
  PostgreSQL hands out a pooled connection per operation and lets the engine
  serialise them.
* A claim therefore cannot lean on the process lock. It takes the row with
  `FOR UPDATE SKIP LOCKED`, so two agents claiming at the same moment take two
  different tasks instead of fighting over one.

The services still speak SQLite's placeholder style (`?` and `:name`), which is
translated here. One dialect in the source, two engines underneath.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator, Mapping, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api import settings
from api.models import schema

log = logging.getLogger("noted.db")

DIALECT = "postgres"

#: Row-level locking is what replaces the single writer: the loser of a race
#: skips the locked row and takes the next one instead of coming back empty.
CLAIM_LOCK = " FOR UPDATE SKIP LOCKED"

#: Read-then-write on one row: compare-and-set is only a guarantee if the row
#: cannot change between the read and the write. Under the process lock that
#: was free; here it has to be asked for.
ROW_LOCK = " FOR UPDATE"

#: How long to wait for the server on the way up. In compose the core starts
#: beside PostgreSQL, and "not accepting connections yet" is not a failure.
STARTUP_WAIT_S = 30.0

#: `:name` but not the second colon of a `::type` cast.
_NAMED = re.compile(r"(?<!:):([a-zA-Z_]\w*)")

_lock = threading.RLock()
_pool: ConnectionPool | None = None
_pool_url: str | None = None


@lru_cache(maxsize=512)
def translate(sql: str) -> str:
    """SQLite placeholders to psycopg ones. Cached: the statements are literals
    in the service modules, so the same handful of strings comes back forever.

    The SQL in this project contains no `%` and no `?` inside a string literal,
    which is what makes a substitution this blunt safe.
    """
    out = sql.replace("%", "%%")
    out = _NAMED.sub(r"%(\1)s", out)
    return out.replace("?", "%s")


class Connection:
    """A psycopg connection that answers to the sqlite3 call style.

    Only `execute` needs adapting: psycopg cursors already give `fetchone`,
    `fetchall`, `rowcount` and iteration, and `dict_row` makes `row["column"]`
    work the way `sqlite3.Row` does.
    """

    __slots__ = ("raw",)

    def __init__(self, raw: psycopg.Connection) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> psycopg.Cursor:
        # params is never None: psycopg only unescapes `%%` when it has
        # parameters to bind, and the translation above always escapes.
        return self.raw.execute(translate(sql), params)


def pool() -> ConnectionPool:
    """The pool, opened once and reopened when NOTED_DB_URL changes."""
    global _pool, _pool_url
    with _lock:
        url = settings.database_url()
        if not url:
            raise RuntimeError("NOTED_DB_URL is not set")
        if _pool is not None and _pool_url == url:
            return _pool
        if _pool is not None:
            _pool.close()
        created = ConnectionPool(
            url,
            min_size=1,
            max_size=settings.db_pool_size(),
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )
        created.open()
        _wait_for_server(created)
        _install(created)
        _pool, _pool_url = created, url
        return created


def _wait_for_server(ready: ConnectionPool) -> None:
    deadline = time.monotonic() + STARTUP_WAIT_S
    while True:
        try:
            with ready.connection(timeout=5.0) as conn:
                conn.execute("SELECT 1")
            return
        except Exception:  # noqa: BLE001 - the server is simply not up yet
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for postgresql…")
            time.sleep(0.5)


def _install(ready: ConnectionPool) -> None:
    """Create what is missing. Safe to run against a populated database: every
    statement is IF NOT EXISTS, and the migration adds columns one by one."""
    with ready.connection() as conn:
        for statement in schema.statements(schema.tables(DIALECT)):
            conn.execute(statement)
        for column, kind in schema.later_columns(DIALECT).items():
            conn.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {column} {kind}")
        for statement in schema.statements(schema.INDEXES):
            conn.execute(statement)


def close() -> None:
    global _pool, _pool_url
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _pool_url = None, None


@contextmanager
def reading() -> Iterator[Connection]:
    with pool().connection() as raw:
        yield Connection(raw)


@contextmanager
def transaction() -> Iterator[Connection]:
    """A write. The pool commits when the block ends and rolls back if it
    raises, so the transaction boundary is the `with` itself."""
    with pool().connection() as raw:
        yield Connection(raw)


def wipe() -> None:
    """Empty every table. For tests; the running service never calls it."""
    with transaction() as conn:
        conn.execute(f"TRUNCATE {', '.join(schema.TABLE_NAMES)} RESTART IDENTITY CASCADE")
