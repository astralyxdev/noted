"""Agents, keys and sessions.

Two things that are easy to confuse:

* **An agent** is who you are. The key proves it, and `assignee_id` follows
  from the key instead of arriving as a parameter: you cannot call yourself by
  somebody else's name.
* **A session** is which instance of that agent you are. The same key can run
  in two processes, and without sessions those two are indistinguishable. A
  task is held by a session, so a zombie on an old session is refused, and the
  death of a process releases everything it held at once.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Sequence

from api import settings
from api.models import database
from api.models.agent import Agent, AgentSession, IssuedKey
from api.models.envelope import Outcome

KEY_PREFIX = "noted_"

#: How long a session survives without renewal. The transport renews it on its
#: own, so the window is short: the shorter it is, the sooner a dead process
#: gives its work back.
DEFAULT_SESSION_TTL_S = 90.0


class AgentError(Exception):
    def __init__(self, code: Outcome, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def session_ttl_s() -> float:
    return settings.session_ttl_s()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hash(key: str) -> str:
    """The key is 256 random bits, so a fast hash is enough: there is nothing
    to guess. Slow KDFs are for passwords, not for issued secrets."""
    return hashlib.sha256(key.encode()).hexdigest()


def _agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        projects=json.loads(row["projects"]) if row["projects"] else None,
        is_admin=bool(row["is_admin"]),
        created_at=_iso(row["created_at"]),
        revoked_at=_iso(row["revoked_at"]),
    )


def _session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=row["id"],
        agent_id=row["agent_id"],
        transport=row["transport"],
        opened_at=_iso(row["opened_at"]),
        renewed_at=_iso(row["renewed_at"]),
        closed_at=_iso(row["closed_at"]),
    )


# ──────────────────────────────── keys ───────────────────────────────────


def admin_exists() -> bool:
    """Is there any way left to reach the dashboard?

    A browser cannot send the header agents use, so the dashboard signs in with
    NOTED_TOKEN or an admin key. Without either, turning identity on would lock
    every human out of their own queue.
    """
    if settings.admin_token():
        return True
    with database.reading() as conn:
        row = conn.execute("SELECT 1 FROM agents WHERE is_admin = 1 AND revoked_at IS NULL LIMIT 1").fetchone()
    return row is not None


def issue(name: str, projects: Sequence[str] | None = None, is_admin: bool = False) -> IssuedKey:
    """Register an agent and issue its key. The key is returned once."""
    agent_id = (name or "").strip()
    if not agent_id:
        raise AgentError(Outcome.validation_error, "an agent name must not be empty")
    if not is_admin and not admin_exists():
        raise AgentError(
            Outcome.validation_error,
            "issuing this key would turn identity on and lock the dashboard out. "
            "Set NOTED_TOKEN, or issue an admin key first: noted-keys add <name> --admin",
        )
    scope = sorted({p.strip() for p in (projects or []) if p.strip()}) or None
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = time.time()

    with database.transaction() as conn:
        existing = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if existing is not None:
            raise AgentError(Outcome.validation_error, f"agent {agent_id} already exists")
        conn.execute(
            "INSERT INTO agents (id, key_hash, projects, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, _hash(key), json.dumps(scope) if scope else None, int(is_admin), now),
        )
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()

    return IssuedKey(agent=_agent(row), key=key)


def revoke(name: str) -> bool:
    """Revoke a key. Its sessions close; the collector frees the tasks."""
    now = time.time()
    with database.transaction() as conn:
        cur = conn.execute("UPDATE agents SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, name))
        if cur.rowcount:
            conn.execute(
                "UPDATE agent_sessions SET closed_at = ? WHERE agent_id = ? AND closed_at IS NULL", (now, name)
            )
        return bool(cur.rowcount)


def list_agents(include_revoked: bool = False) -> list[Agent]:
    sql = "SELECT * FROM agents"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    sql += " ORDER BY id"
    with database.reading() as conn:
        return [_agent(r) for r in conn.execute(sql)]


def resolve(key: str | None) -> Agent | None:
    """Key to agent. Revoked and unknown keys both give None."""
    if not key:
        return None
    with database.reading() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE key_hash = ? AND revoked_at IS NULL", (_hash(key),)
        ).fetchone()
    return _agent(row) if row else None


def identity_required() -> bool:
    """Until an agent exists, the core runs as an open queue.

    Issuing the first key turns identity on, so an upgrade breaks no local
    install and whoever wants strictness gets it by an explicit act.

    Revoked keys count too. Otherwise revoking the last key would silently
    reopen the queue to everyone — at exactly the moment it is being closed.
    """
    with database.reading() as conn:
        row = conn.execute("SELECT 1 FROM agents LIMIT 1").fetchone()
    return row is not None


# ─────────────────────────────── sessions ────────────────────────────────


def open_session(agent_id: str, transport: str | None = None) -> AgentSession:
    session_id = secrets.token_urlsafe(18)
    now = time.time()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent_id, transport, opened_at, renewed_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_id, transport, now, now),
        )
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session(row)


def ensure_session(session_id: str, agent_id: str, transport: str | None = None) -> AgentSession | None:
    """A session under a given id, or None when the id may not be used.

    For the MCP transport that id is Mcp-Session-Id: the client's session is the
    agent's session, and nothing has to be opened by hand. Two rules keep that
    from becoming a hole, because session ids travel in headers and are easy to
    observe:

    * a session belongs to one agent and nobody else may present its id;
    * a session that was closed **deliberately** — on logout, or when the key was
      revoked — stays closed.

    Going quiet is not the same as being closed. An HTTP client sends nothing
    during a long step, and its session goes stale; refusing it afterwards would
    lock a live agent out of its own queue for ever, since the client keeps
    presenting the same id. Staleness costs it the tasks it was holding — the
    collector takes those — but not the right to carry on working.
    """
    now = time.time()
    ttl = session_ttl_s()

    with database.reading() as conn:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()

    if row is not None:
        if row["agent_id"] != agent_id or row["closed_at"] is not None:
            return None
        # Renewal is a write under the global lock, and every request carries a
        # session. Touch the row only once per third of the TTL.
        if now - row["renewed_at"] <= ttl / 3:
            return _session(row)

    with database.transaction() as conn:
        current = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        if current is None:
            conn.execute(
                "INSERT INTO agent_sessions (id, agent_id, transport, opened_at, renewed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, agent_id, transport, now, now),
            )
        elif current["agent_id"] != agent_id or current["closed_at"] is not None:
            return None
        else:
            conn.execute("UPDATE agent_sessions SET renewed_at = ? WHERE id = ?", (now, session_id))
        return _session(conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone())


def renew_session(session_id: str) -> bool:
    """Renew a session. Called by the transport, not by the model: an agent
    should not have to remember "I am alive" while a ten-minute build runs."""
    now = time.time()
    with database.transaction() as conn:
        cur = conn.execute(
            "UPDATE agent_sessions SET renewed_at = ? WHERE id = ? AND closed_at IS NULL", (now, session_id)
        )
        return bool(cur.rowcount)


def close_session(session_id: str) -> bool:
    now = time.time()
    with database.transaction() as conn:
        cur = conn.execute(
            "UPDATE agent_sessions SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (now, session_id)
        )
        return bool(cur.rowcount)


def alive(session_id: str | None) -> bool:
    if not session_id:
        return False
    with database.reading() as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_sessions WHERE id = ? AND closed_at IS NULL AND renewed_at > ?",
            (session_id, time.time() - session_ttl_s()),
        ).fetchone()
    return row is not None


def get_session(session_id: str) -> AgentSession | None:
    with database.reading() as conn:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session(row) if row else None


def prune_sessions(older_than_days: float = 30.0) -> int:
    """Drop long-dead session rows. Liveness itself is read from `renewed_at`:
    a session is not closed for going quiet, or a live client that spent ten
    minutes on one step could never speak again.
    """
    cutoff = time.time() - older_than_days * 86_400
    with database.transaction() as conn:
        cur = conn.execute("DELETE FROM agent_sessions WHERE renewed_at < ?", (cutoff,))
        return cur.rowcount


def sessions_of(agent_id: str, only_open: bool = True) -> list[AgentSession]:
    sql = "SELECT * FROM agent_sessions WHERE agent_id = ?"
    if only_open:
        sql += " AND closed_at IS NULL"
    sql += " ORDER BY opened_at DESC"
    with database.reading() as conn:
        return [_session(r) for r in conn.execute(sql, (agent_id,))]
