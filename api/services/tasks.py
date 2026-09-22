"""Логика домена задач. Здесь живёт весь SQL.

Модуль сознательно не знает про FastAPI: возвращает данные и поднимает
TaskError, а как это превратить в HTTP — дело слоя доставки. Поэтому один и
тот же код обслуживает и JSON-API, и MCP, и дэшборд, и тесты без поднятия
приложения.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from api.models import database
from api.models.envelope import Outcome
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
    """Ошибка, понятная вызывающему слою: code — это значение Outcome."""

    def __init__(self, code: Outcome, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─────────────────────────────── настройки ───────────────────────────────


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def default_lease_s() -> float:
    return _env_float("NOTED_LEASE_S", DEFAULT_LEASE_S)


def retry_base_s() -> float:
    """Первая пауза перед повтором; дальше удваивается."""
    return _env_float("NOTED_RETRY_BASE_S", 5.0)


def retry_cap_s() -> float:
    return _env_float("NOTED_RETRY_CAP_S", 300.0)


def create_limit() -> int:
    """Сколько задач один автор может поставить за окно. 0 — без ограничения.

    Смысл не в квотах, а в предохранителе: очередь открыта, ставить может кто
    угодно, и зациклившийся агент зальёт её тысячей задач за секунды. Предел
    взят с запасом, чтобы честная декомпозиция работы в сотню подзадач прошла,
    а патология — нет.
    """
    try:
        return max(0, int(os.environ.get("NOTED_CREATE_LIMIT", "") or 300))
    except ValueError:
        return 300


def create_window_s() -> float:
    return _env_float("NOTED_CREATE_WINDOW_S", 60.0)


def _backoff(attempt: int) -> float:
    return min(retry_base_s() * (2 ** max(attempt - 1, 0)), retry_cap_s())


# ──────────────────────────── преобразования ─────────────────────────────


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _actor(value: ActorId | None, field: str) -> str | None:
    """assignee_id, created_by и project приходят строкой или числом — храним TEXT."""
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
    lease = default_lease_s() if value is None else float(value)
    return max(0.0, min(lease, MAX_LEASE_S))


#: Подзапрос счёта незакрытых зависимостей — чтобы список не делал N+1.
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
    """Запись в журнал. Пишется в той же транзакции, что и само изменение,
    иначе журнал разойдётся с состоянием."""
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


# ──────────────────────────────── создание ────────────────────────────────


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
    """Создать задачу. Второй элемент — создана ли она на самом деле: при
    совпадении `key` возвращается существующая, чтобы ретрай агента не плодил дубли."""
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
        # Идемпотентный повтор ничего не создаёт, поэтому и лимитом не режется.
        if key is not None:
            already = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            if already is not None:
                return _task(conn, already), False

        limit = create_limit()
        if limit:
            window = create_window_s()
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
        # Цикл собрать нельзя: на новую задачу ещё никто не ссылается.
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


# ──────────────────────────────── чтение ─────────────────────────────────


def get(task_id: int) -> Task | None:
    with database.reading() as conn:
        row = _fetch(conn, task_id)
        return _task(conn, row) if row is not None else None


def events(task_id: int, limit: int = 200) -> list[TaskEvent]:
    """Журнал переходов задачи, от старых к новым."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id LIMIT ?", (task_id, limit)
        ).fetchall()
    return [_event(r) for r in rows]


def list_tasks(
    assignee_id: ActorId | None = None,
    status: Status | str | Sequence[Status | str] | None = None,
    project: str | None = None,
    unscoped: bool = False,
    unassigned: bool = False,
    parent_id: int | None = None,
    stale_seconds: float | None = None,
    limit: int = 50,
) -> list[TaskSummary]:
    """Список, свежие первыми. Без `result` — за ним идут в get()."""
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

    sql = f"SELECT tasks.*, {UNMET_DEPS} AS waiting_on FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), MAX_LIMIT)))

    with database.reading() as conn:
        return [_summary(r) for r in conn.execute(sql, args)]


# ─────────────────────────── изменение состояния ──────────────────────────


def _unblock_dependents(conn: sqlite3.Connection, task_id: int, now: float) -> list[int]:
    """Предшественник закрыт успешно — снимаем блокировку с тех, кому больше нечего ждать."""
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
    """Предшественник провалился или отменён — ждущие его больше не поедут сами."""
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
) -> tuple[Outcome, Task | None]:
    """Сменить статус, при необходимости приложив результат.

    if_status делает вызов compare-and-set: запись проходит только если задача
    всё ещё в этом статусе. Так двое не доделывают одну задачу.

    actor — кто меняет. Если задача в работе у другого исполнителя, придёт
    not_owner: агент не должен закрывать чужую работу по ошибке.

    Если задача провалена, но попытки ещё есть (`max_attempts`), она не
    остаётся в failed, а возвращается в очередь с паузой — это и есть ретрай.
    """
    new_status = _status(status)
    expected = _status(if_status, "if_status") if if_status is not None else None
    who = _actor(actor, "actor")
    payload = _dump(result, "result") if result is not None else None

    with database.transaction() as conn:
        row = _fetch(conn, task_id)
        if row is None:
            return Outcome.not_found, None
        if expected is not None and row["status"] != expected.value:
            return Outcome.status_conflict, _task(conn, row)
        if who is not None and row["status"] == Status.in_progress.value and row["assignee_id"] not in (None, who):
            return Outcome.not_owner, _task(conn, row)

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
                   lease_expires = CASE WHEN ? THEN NULL ELSE lease_expires END
             WHERE id = ?
            """,
            (new_status.value, payload, now, new_status in TERMINAL, task_id),
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


def claim(assignee_id: ActorId, project: str | None = None, lease_s: float | None = None) -> Task | None:
    """Атомарно забрать одну pending-задачу и перевести её в in_progress.

    Порядок: сначала адресованные этому исполнителю, потом общий пул; внутри —
    по убыванию приоритета, при равном приоритете FIFO.

    Не выдаются: задачи с неистёкшей паузой после провала и те, у которых
    остались незакрытые зависимости.

    `lease_s` — срок аренды. По истечении задача вернётся в очередь сама,
    если исполнитель не продлил её heartbeat-ом. `lease_s=0` — без аренды.
    """
    assignee = _actor(assignee_id, "assignee_id")
    if assignee is None:
        raise TaskError(Outcome.validation_error, "assignee_id обязателен для захвата задачи")
    scope = _actor(project, "project")
    lease = _lease(lease_s)
    now = time.time()

    with database.transaction() as conn:
        row = conn.execute(
            """
            SELECT id FROM tasks AS t
             WHERE t.status = 'pending'
               AND (t.assignee_id IS NULL OR t.assignee_id = :me)
               AND (:scope IS NULL OR t.project = :scope)
               AND (t.retry_after IS NULL OR t.retry_after <= :now)
               AND NOT EXISTS (
                     SELECT 1 FROM task_deps d JOIN tasks p ON p.id = d.depends_on_id
                      WHERE d.task_id = t.id AND p.status <> 'done')
             ORDER BY (t.assignee_id IS NULL), t.priority DESC, t.id
             LIMIT 1
            """,
            {"me": assignee, "scope": scope, "now": now},
        ).fetchone()
        if row is None:
            return None

        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'in_progress', assignee_id = ?, attempts = attempts + 1,
                   lease_expires = ?, retry_after = NULL, updated_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (assignee, (now + lease) if lease else None, now, row["id"]),
        )
        if cur.rowcount == 0:
            return None

        taken = _fetch(conn, row["id"])
        _log(conn, row["id"], "claimed", actor=assignee, from_status="pending",
             to_status="in_progress",
             detail={"attempt": taken["attempts"], "lease_s": lease or None}, at=now)
        return _task(conn, taken)


def heartbeat(task_id: int, assignee_id: ActorId, lease_s: float | None = None) -> tuple[Outcome, Task | None]:
    """Продлить аренду. Исполнитель говорит «я жив и всё ещё делаю эту задачу».

    Без этого долгая задача вернётся в очередь по истечении аренды — что и
    нужно, когда агент умер, и чего не нужно, когда он просто долго работает.
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

        conn.execute(
            "UPDATE tasks SET lease_expires = ?, updated_at = ? WHERE id = ?",
            ((now + lease) if lease else None, now, task_id),
        )
        return Outcome.updated, _task(conn, _fetch(conn, task_id))


def reap_expired() -> list[int]:
    """Вернуть в очередь задачи с истёкшей арендой.

    Это то, что отличает «видно, что залипло» от «само починилось»: агент умер,
    аренда истекла — задача снова доступна. Если попытки исчерпаны, задача
    уходит в failed, иначе её будут воскрешать бесконечно.
    """
    now = time.time()
    requeued: list[int] = []

    with database.transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, assignee_id, attempts, max_attempts FROM tasks
             WHERE status = 'in_progress' AND lease_expires IS NOT NULL AND lease_expires <= ?
            """,
            (now,),
        ).fetchall()

        for row in rows:
            exhausted = row["max_attempts"] is not None and row["attempts"] >= row["max_attempts"]
            if exhausted:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'failed', lease_expires = NULL, updated_at = ?,
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
                       SET status = 'pending', assignee_id = NULL, lease_expires = NULL, updated_at = ?
                     WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                _log(conn, row["id"], "reaped", actor="system", from_status="in_progress",
                     to_status="pending", detail={"held_by": row["assignee_id"], "attempts": row["attempts"]}, at=now)
                requeued.append(row["id"])

    return requeued


# ──────────────────────────────── сводки ─────────────────────────────────


def stats(project: str | None = None, unscoped: bool = False) -> dict[str, int]:
    """Счётчики по статусам для чипов дэшборда и /api/stats.

    Считаются внутри текущего скоупа: иначе при выбранном проекте чипы врут —
    показывают «все 8», когда в списке четыре задачи.
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
    """Известные проекты — для фильтров дэшборда и подсказок в форме."""
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) AS n FROM tasks WHERE project IS NOT NULL GROUP BY project ORDER BY project"
        ).fetchall()
    return [r["project"] for r in rows]


def assignees() -> list[str]:
    """Известные исполнители — для выпадающего списка фильтров."""
    with database.reading() as conn:
        rows = conn.execute(
            "SELECT DISTINCT assignee_id FROM tasks WHERE assignee_id IS NOT NULL ORDER BY assignee_id"
        ).fetchall()
    return [r["assignee_id"] for r in rows]
