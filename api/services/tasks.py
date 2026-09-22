"""Task domain logic. All the SQL lives here.

The module knows nothing about FastAPI on purpose: it returns data and raises
TaskError, and turning that into HTTP is the delivery layer's job. That is why
the same code serves the JSON API, MCP, the dashboard, and tests that never
start the application.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from api import settings
from api.models import database
from api.models.envelope import Outcome
from api.services import agents
from api.models.task import (
    DEFAULT_LEASE_S,
    MAX_LEASE_S,
    MAX_LIMIT,
    TERMINAL,
    ActorId,
    Status,
    Task,
    TaskEvent,
    TaskSummary,
)


class TaskError(Exception):
    """An error the calling layer understands: `code` is an Outcome value."""

    def __init__(self, code: Outcome, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _backoff(attempt: int) -> float:
    return min(settings.retry_base_s() * (2 ** max(attempt - 1, 0)), settings.retry_cap_s())


# ──────────────────────────── conversions ───────────────────────────────


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _actor(value: ActorId | None, field: str) -> str | None:
    """assignee_id, created_by and project arrive as a string or a number; stored as TEXT."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TaskError(Outcome.validation_error, f"{field} должен быть строкой или числом")
    out = str(value).strip()
    if not out:
        raise TaskError(Outcome.validation_error, f"{field} не может быть пустым (для «без значения» передайте null)")
    return out


def _status(value: Status | str, field: str = "status") -> Status:
    try:
        return Status(value)
    except ValueError:
        allowed = ", ".join(s.value for s in Status)
        raise TaskError(Outcome.validation_error, f"{field}: ожидалось одно из {allowed}, получено {value!r}") from None


def _dump(value: Any, field: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TaskError(Outcome.validation_error, f"{field} должен сериализоваться в JSON: {exc}") from exc


def _lease(value: float | None) -> float:
    lease = settings.lease_s() if value is None else float(value)
    return max(0.0, min(lease, MAX_LEASE_S))


#: Subquery counting unmet dependencies, so listing does not turn into N+1.
UNMET_DEPS = """(SELECT COUNT(*) FROM task_deps d JOIN tasks p ON p.id = d.depends_on_id
                  WHERE d.task_id = tasks.id AND p.status <> 'done')"""


def _fields(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "waiting_on": row["waiting_on"] if "waiting_on" in keys else 0,
        "id": row["id"],
        "task": json.loads(row["task"]),
        "status": row["status"],
        "project": row["project"],
        "assignee_id": row["assignee_id"],
        "created_by": row["created_by"],
        "parent_id": row["parent_id"],
        "key": row["key"],
        "priority": row["priority"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "retry_after": _iso(row["retry_after"]),
        "lease_expires": _iso(row["lease_expires"]),
        "session_id": row["session_id"] if "session_id" in row.keys() else None,
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _task(conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
    deps = [r["depends_on_id"] for r in conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_id", (row["id"],)
    )]
    unmet = conn.execute(
        "SELECT COUNT(*) AS n FROM task_deps d JOIN tasks p ON p.id = d.depends_on_id "
        " WHERE d.task_id = ? AND p.status <> 'done'",
        (row["id"],),
    ).fetchone()["n"]
    return Task(
        **{**_fields(row), "waiting_on": unmet},
        result=json.loads(row["result"]) if row["result"] is not None else None,
        depends_on=deps,
    )


def _summary(row: sqlite3.Row) -> TaskSummary:
    return TaskSummary(**_fields(row))


def _event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=row["id"],
        task_id=row["task_id"],
        at=_iso(row["at"]),
        event=row["event"],
        actor=row["actor"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        detail=json.loads(row["detail"]) if row["detail"] is not None else None,
    )


def _fetch(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _log(
    conn: sqlite3.Connection,
    task_id: int,
    event: str,
    *,
    actor: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: Any = None,
    at: float | None = None,
) -> None:
    """A journal entry, written in the same transaction as the change itself;
    otherwise the journal drifts away from the state."""
    conn.execute(
        """
        INSERT INTO task_events (task_id, at, event, actor, from_status, to_status, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            at if at is not None else time.time(),
            event,
            actor,
            from_status,
            to_status,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        ),
    )


# ──────────────────────────────── creating ───────────────────────────────


def create(
    task: Any,
    project: str | None = None,
    assignee_id: ActorId | None = None,
    parent_id: int | None = None,
    key: str | None = None,
    created_by: ActorId | None = None,
    priority: int = 0,
    max_attempts: int | None = None,
    depends_on: Sequence[int] | None = None,
) -> tuple[Task, bool]:
    """Create a task. The second element says whether it was really created:
    a matching `key` returns the existing task so a retry breeds no duplicates."""
    if not isinstance(task, dict):
        raise TaskError(Outcome.validation_error, "task должен быть JSON-объектом")
    payload = _dump(task, "task")
    scope = _actor(project, "project")
    assignee = _actor(assignee_id, "assignee_id")
    creator = _actor(created_by, "created_by")
    if key is not None and not str(key).strip():
        raise TaskError(Outcome.validation_error, "key не может быть пустым (для «без ключа» передайте null)")
    if max_attempts is not None and max_attempts < 1:
        raise TaskError(Outcome.validation_error, "max_attempts должен быть не меньше 1")
    wanted_deps = sorted({int(d) for d in (depends_on or [])})

    now = time.time()
    with database.transaction() as conn:
        # An idempotent repeat creates nothing, so the rate limit must not cut it.
        if key is not None:
            already = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            if already is not None:
                return _task(conn, already), False

        limit = settings.create_limit()
        if limit:
            window = settings.create_window_s()
            recent = conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE created_at > ? AND created_by IS {'NOT NULL AND created_by = ?' if creator else 'NULL'}",
                (now - window, creator) if creator else (now - window,),
            ).fetchone()["n"]
            if recent >= limit:
                who = creator or "без автора"
                raise TaskError(
                    Outcome.rate_limited,
                    f"{who}: {recent} задач за последние {int(window)} с при пределе {limit} — притормозите",
                )

        if parent_id is not None and _fetch(conn, parent_id) is None:
            raise TaskError(Outcome.parent_not_found, f"родительской задачи {parent_id} не существует")
        for dep in wanted_deps:
            if _fetch(conn, dep) is None:
                raise TaskError(Outcome.validation_error, f"зависимости {dep} не существует")

        cur = conn.execute(
            """
            INSERT INTO tasks (task, status, project, assignee_id, created_by, parent_id, key,
                               priority, max_attempts, created_at, updated_at)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (payload, scope, assignee, creator, parent_id, key, int(priority), max_attempts, now, now),
        )
        if cur.rowcount == 0:
            existing = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            return _task(conn, existing), False

        task_id = cur.lastrowid
        # No cycle can form: nothing references a brand new task yet.
        for dep in wanted_deps:
            conn.execute(
                "INSERT OR IGNORE INTO task_deps (task_id, depends_on_id) VALUES (?, ?)", (task_id, dep)
            )
        _log(
            conn,
            task_id,
            "created",
            actor=creator,
            to_status=Status.pending.value,
            detail={"project": scope, "priority": int(priority), "depends_on": wanted_deps} if (scope or priority or wanted_deps) else None,
            at=now,
        )
        return _task(conn, _fetch(conn, task_id)), True


# ──────────────────────────────── reading ────────────────────────────────


def get(task_id: int) -> Task | None:
    with database.reading() as conn:
        row = _fetch(conn, task_id)
        return _task(conn, row) if row is not None else None


def events(task_id: int, limit: int = 200) -> list[TaskEvent]:
    """The transition journal of one task, oldest first."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id LIMIT ?", (task_id, limit)
        ).fetchall()
    return [_event(r) for r in rows]


def events_after(cursor: int = 0, limit: int = 200) -> list[TaskEvent]:
    """The whole journal, starting after the given entry number.

    Lossless reconnection rests on this: a client remembers the number of the
    last event it saw and asks to continue from there.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE id > ? ORDER BY id LIMIT ?", (int(cursor), limit)
        ).fetchall()
    return [_event(r) for r in rows]


def last_event_id() -> int:
    with database.reading() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS last FROM task_events").fetchone()
    return row["last"]


def list_tasks(
    assignee_id: ActorId | None = None,
    status: Status | str | Sequence[Status | str] | None = None,
    project: str | None = None,
    unscoped: bool = False,
    unassigned: bool = False,
    parent_id: int | None = None,
    stale_seconds: float | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[TaskSummary]:
    """The list, newest first. No `result` — that is what get() is for.

    `before_id` is the paging cursor: the next page is everything older than
    the last task shown. A cursor over `id` rather than an offset, because new
    tasks keep arriving while somebody scrolls, and an offset would start
    skipping rows.
    """
    where: list[str] = []
    args: list[Any] = []

    if unscoped:
        where.append("project IS NULL")
    elif project is not None:
        where.append("project = ?")
        args.append(_actor(project, "project"))

    if unassigned:
        where.append("assignee_id IS NULL")
    elif assignee_id is not None:
        where.append("assignee_id = ?")
        args.append(_actor(assignee_id, "assignee_id"))

    if status is not None:
        wanted = [status] if isinstance(status, (str, Status)) else list(status)
        if not wanted:
            raise TaskError(Outcome.validation_error, "фильтр status не может быть пустым списком")
        values = [_status(s).value for s in wanted]
        where.append(f"status IN ({','.join('?' * len(values))})")
        args.extend(values)

    if parent_id is not None:
        where.append("parent_id = ?")
        args.append(parent_id)

    if stale_seconds is not None:
        if stale_seconds < 0:
            raise TaskError(Outcome.validation_error, "stale_seconds не может быть отрицательным")
        where.append("updated_at <= ?")
        args.append(time.time() - stale_seconds)

    if before_id is not None:
        where.append("id < ?")
        args.append(int(before_id))

    sql = f"SELECT tasks.*, {UNMET_DEPS} AS waiting_on FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), MAX_LIMIT)))

    with database.reading() as conn:
        return [_summary(r) for r in conn.execute(sql, args)]


# ─────────────────────────── changing state ──────────────────────────────


def _unblock_dependents(conn: sqlite3.Connection, task_id: int, now: float) -> list[int]:
    """A predecessor closed successfully: unblock whoever has nothing left to wait for."""
    rows = conn.execute(
        """
        SELECT t.id FROM tasks t
          JOIN task_deps d ON d.task_id = t.id
         WHERE d.depends_on_id = ?
           AND t.status = 'blocked'
           AND NOT EXISTS (SELECT 1 FROM task_deps dd JOIN tasks p ON p.id = dd.depends_on_id
                            WHERE dd.task_id = t.id AND p.status <> 'done')
        """,
        (task_id,),
    ).fetchall()
    freed = [r["id"] for r in rows]
    for dependent in freed:
        conn.execute("UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?", (now, dependent))
        _log(conn, dependent, "unblocked", actor="system", from_status="blocked",
             to_status="pending", detail={"after": task_id}, at=now)
    return freed


def _block_dependents(conn: sqlite3.Connection, task_id: int, reason: str, now: float) -> list[int]:
    """A predecessor failed or was cancelled: its waiters will not move on their own."""
    rows = conn.execute(
        "SELECT t.id FROM tasks t JOIN task_deps d ON d.task_id = t.id "
        " WHERE d.depends_on_id = ? AND t.status = 'pending'",
        (task_id,),
    ).fetchall()
    blocked = [r["id"] for r in rows]
    for dependent in blocked:
        conn.execute("UPDATE tasks SET status = 'blocked', updated_at = ? WHERE id = ?", (now, dependent))
        _log(conn, dependent, "blocked", actor="system", from_status="pending",
             to_status="blocked", detail={"because": task_id, "reason": reason}, at=now)
    return blocked


def set_status(
    task_id: int,
    status: Status | str,
    result: Any = None,
    if_status: Status | str | None = None,
    actor: ActorId | None = None,
    session_id: str | None = None,
    force: bool = False,
) -> tuple[Outcome, Task | None]:
    """Change the status, optionally attaching a result.

    **Compare-and-set is on by default.** With no `if_status` the server fills
    in `in_progress`: the only state an executor may report from. A forgotten
    `if_status` no longer breaks invariants in silence; an unconditional write
    is an explicit `force`, and that is a human acting from the dashboard, not
    an agent overlooking a parameter.

    `actor` is who is writing. A task held by somebody else answers not_owner:
    an agent must not close another's work by mistake.

    When a task fails but attempts remain (`max_attempts`), it does not stay
    failed — it returns to the queue after a pause. That is the retry.
    """
    new_status = _status(status)
    if if_status is not None:
        expected: Status | None = _status(if_status, "if_status")
    elif force:
        expected = None
    else:
        expected = Status.in_progress
    who = _actor(actor, "actor")
    payload = _dump(result, "result") if result is not None else None

    with database.transaction() as conn:
        row = _fetch(conn, task_id)
        if row is None:
            return Outcome.not_found, None
        if expected is not None and row["status"] != expected.value:
            return Outcome.status_conflict, _task(conn, row)
        # Finishing releases the session: otherwise a human cancels a task and a
        # late agent silently overwrites that cancellation with its own done.

        if who is not None and row["status"] == Status.in_progress.value and row["assignee_id"] not in (None, who):
            return Outcome.not_owner, _task(conn, row)
        # Fencing: a task is held by one instance of an agent, not by its name.
        # A zombie on an old session is refused even when the name matches.
        if session_id is not None and row["session_id"] is not None and row["session_id"] != session_id:
            return Outcome.stale_session, _task(conn, row)

        now = time.time()
        retrying = (
            new_status is Status.failed
            and row["max_attempts"] is not None
            and row["attempts"] < row["max_attempts"]
        )

        if retrying:
            delay = _backoff(row["attempts"])
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'pending', assignee_id = NULL, lease_expires = NULL,
                       retry_after = ?, result = COALESCE(?, result), updated_at = ?
                 WHERE id = ?
                """,
                (now + delay, payload, now, task_id),
            )
            _log(conn, task_id, "retry", actor=who or row["assignee_id"], from_status=row["status"],
                 to_status=Status.pending.value,
                 detail={"attempt": row["attempts"], "of": row["max_attempts"], "in_s": round(delay, 1)}, at=now)
            return Outcome.updated, _task(conn, _fetch(conn, task_id))

        conn.execute(
            """
            UPDATE tasks
               SET status = ?, result = COALESCE(?, result), updated_at = ?,
                   lease_expires = CASE WHEN ? THEN NULL ELSE lease_expires END,
                   session_id = CASE WHEN ? THEN NULL ELSE session_id END
             WHERE id = ?
            """,
            (new_status.value, payload, now, new_status in TERMINAL, new_status in TERMINAL, task_id),
        )
        _log(conn, task_id, "status", actor=who or row["assignee_id"], from_status=row["status"],
             to_status=new_status.value,
             detail={"dead_letter": True} if (new_status is Status.failed and row["max_attempts"] is not None) else None,
             at=now)

        if new_status is Status.done:
            _unblock_dependents(conn, task_id, now)
        elif new_status in (Status.failed, Status.cancelled):
            _block_dependents(conn, task_id, new_status.value, now)

        return Outcome.updated, _task(conn, _fetch(conn, task_id))


def claim(
    assignee_id: ActorId,
    project: str | None = None,
    lease_s: float | None = None,
    session_id: str | None = None,
    allowed_projects: Sequence[str] | None = None,
) -> Task | None:
    """Atomically take one pending task and move it to in_progress.

    Dispatch order is a contract, not an implementation detail:
    `priority` descending → addressed before the pool → FIFO by `id`.

    Priority comes first on purpose: otherwise a low-priority task addressed to
    an agent would overtake an urgent one from the pool, and urgency would stop
    meaning anything. Starvation is possible and that is a deliberate choice —
    a stream of high-priority work can hold off the low-priority indefinitely.

    Never handed out: tasks still inside their post-failure pause, and tasks
    whose dependencies are not closed.

    `lease_s` is the lease length. When it expires the task returns to the
    queue on its own unless the executor renewed it. `lease_s=0` means none.

    Inside a session no lease is set at all: the task is held for as long as
    the session lives. That lifts heartbeats off the model, which cannot renew
    anything while a ten-minute build runs, and puts them on the transport,
    which is a separate living process.
    """
    assignee = _actor(assignee_id, "assignee_id")
    if assignee is None:
        raise TaskError(Outcome.validation_error, "assignee_id обязателен для захвата задачи")
    scope = _actor(project, "project")
    # Inside a session no lease is needed: the session holds it, and the
    # transport is what renews the session.
    lease = (float(lease_s) if lease_s is not None else 0.0) if session_id else _lease(lease_s)
    now = time.time()

    allowed = sorted({str(p) for p in allowed_projects}) if allowed_projects is not None else None
    if allowed is not None and scope is not None and scope not in allowed:
        raise TaskError(Outcome.forbidden, f"проект {scope} вне скоупа ключа")

    scope_sql = ""
    params: dict[str, Any] = {"me": assignee, "scope": scope, "now": now}
    if allowed is not None and scope is None:
        # A scoped key takes from its own projects — and from the shared pool.
        names = {f"p{i}": name for i, name in enumerate(allowed)}
        params.update(names)
        listed = ", ".join(f":{k}" for k in names) or "NULL"
        scope_sql = f" AND (t.project IS NULL OR t.project IN ({listed}))"

    with database.transaction() as conn:
        row = conn.execute(
            f"""
            SELECT id FROM tasks AS t
             WHERE t.status = 'pending'
               AND (t.assignee_id IS NULL OR t.assignee_id = :me)
               AND (:scope IS NULL OR t.project = :scope)
               {scope_sql}
               AND (t.retry_after IS NULL OR t.retry_after <= :now)
               AND NOT EXISTS (
                     SELECT 1 FROM task_deps d JOIN tasks p ON p.id = d.depends_on_id
                      WHERE d.task_id = t.id AND p.status <> 'done')
             ORDER BY t.priority DESC, (t.assignee_id IS NULL), t.id
             LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None

        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'in_progress', assignee_id = ?, attempts = attempts + 1,
                   lease_expires = ?, session_id = ?, retry_after = NULL, updated_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (assignee, (now + lease) if lease else None, session_id, now, row["id"]),
        )
        if cur.rowcount == 0:
            return None

        taken = _fetch(conn, row["id"])
        _log(conn, row["id"], "claimed", actor=assignee, from_status="pending",
             to_status="in_progress",
             detail={"attempt": taken["attempts"], "lease_s": lease or None,
                     "session": session_id}, at=now)
        return _task(conn, taken)


def heartbeat(
    task_id: int,
    assignee_id: ActorId,
    lease_s: float | None = None,
    session_id: str | None = None,
) -> tuple[Outcome, Task | None]:
    """Extend the lease: the executor says "I am alive and still on this task".

    Without it a long task returns to the queue when the lease expires — which
    is what you want when the agent died, and not what you want when it is
    simply taking its time.
    """
    who = _actor(assignee_id, "assignee_id")
    lease = _lease(lease_s)
    now = time.time()

    with database.transaction() as conn:
        row = _fetch(conn, task_id)
        if row is None:
            return Outcome.not_found, None
        if row["status"] != Status.in_progress.value:
            return Outcome.status_conflict, _task(conn, row)
        if row["assignee_id"] != who:
            return Outcome.not_owner, _task(conn, row)
        if session_id is not None and row["session_id"] is not None and row["session_id"] != session_id:
            return Outcome.stale_session, _task(conn, row)

        conn.execute(
            "UPDATE tasks SET lease_expires = ?, updated_at = ? WHERE id = ?",
            ((now + lease) if lease else None, now, task_id),
        )
        return Outcome.updated, _task(conn, _fetch(conn, task_id))


def reap_expired() -> list[int]:
    """Return tasks whose lease expired, or whose session died, to the queue.

    This is the difference between "you can see it is stuck" and "it fixed
    itself": the agent died, the lease ran out, the work is available again.
    When attempts are exhausted the task goes to failed instead, or it would be
    resurrected forever.
    """
    now = time.time()
    requeued: list[int] = []
    ttl = settings.session_ttl_s()

    with database.transaction() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.assignee_id, t.attempts, t.max_attempts,
                   t.session_id IS NOT NULL AS by_session
              FROM tasks t
              LEFT JOIN agent_sessions s ON s.id = t.session_id
             WHERE t.status = 'in_progress'
               AND (
                     (t.lease_expires IS NOT NULL AND t.lease_expires <= :now)
                     -- The session died: release everything it held at once.
                     OR (t.session_id IS NOT NULL
                         AND (s.id IS NULL OR s.closed_at IS NOT NULL OR s.renewed_at <= :stale))
                   )
            """,
            {"now": now, "stale": now - ttl},
        ).fetchall()

        for row in rows:
            exhausted = row["max_attempts"] is not None and row["attempts"] >= row["max_attempts"]
            if exhausted:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'failed', lease_expires = NULL, session_id = NULL, updated_at = ?,
                           result = COALESCE(result, ?)
                     WHERE id = ?
                    """,
                    (now, _dump({"error": "аренда истекла, попытки исчерпаны"}, "result"), row["id"]),
                )
                _log(conn, row["id"], "dead_letter", actor="system", from_status="in_progress",
                     to_status="failed", detail={"held_by": row["assignee_id"], "attempts": row["attempts"]}, at=now)
                _block_dependents(conn, row["id"], "failed", now)
            else:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'pending', assignee_id = NULL, lease_expires = NULL,
                           session_id = NULL, updated_at = ?
                     WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                _log(conn, row["id"], "reaped", actor="system", from_status="in_progress",
                     to_status="pending",
                     detail={"held_by": row["assignee_id"], "attempts": row["attempts"],
                             "reason": "сессия умерла" if row["by_session"] else "аренда истекла"}, at=now)
                requeued.append(row["id"])

    return requeued


# ──────────────────────────────── summaries ──────────────────────────────


def trim_journal() -> int:
    """Trim the journal. It is append-only, so it grows forever — which SQLite
    tolerates for a long time, but not indefinitely.

    Entries of closed tasks are dropped by age; the journal of a live task is
    never touched however old it is, because that is what an ongoing incident
    is reconstructed from.
    """
    keep = settings.journal_keep_days()
    if keep <= 0:
        return 0
    cutoff = time.time() - keep * 86_400
    with database.transaction() as conn:
        cur = conn.execute(
            """
            DELETE FROM task_events
             WHERE at < ?
               AND task_id IN (SELECT id FROM tasks WHERE status IN ('done', 'failed', 'cancelled'))
            """,
            (cutoff,),
        )
        return cur.rowcount


def stats(project: str | None = None, unscoped: bool = False) -> dict[str, int]:
    """Per-status counters for the dashboard chips and /api/stats.

    Counted inside the current scope: otherwise, with a project selected, the
    chips lie — "all 8" above a list of four.
    """
    where, args = "", []
    if unscoped:
        where = " WHERE project IS NULL"
    elif project is not None:
        where = " WHERE project = ?"
        args.append(_actor(project, "project"))

    with database.reading() as conn:
        rows = conn.execute(f"SELECT status, COUNT(*) AS n FROM tasks{where} GROUP BY status", args).fetchall()
    counts = {s.value: 0 for s in Status}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["total"] = sum(counts[s.value] for s in Status)
    return counts


def projects() -> list[str]:
    """Known projects, for the dashboard filters and the new-task form."""
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) AS n FROM tasks WHERE project IS NOT NULL GROUP BY project ORDER BY project"
        ).fetchall()
    return [r["project"] for r in rows]


def assignees() -> list[str]:
    """Known assignees, for the filter dropdown."""
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT DISTINCT assignee_id FROM tasks WHERE assignee_id IS NOT NULL ORDER BY assignee_id"
        ).fetchall()
    return [r["assignee_id"] for r in rows]
