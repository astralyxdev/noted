"""Логика домена задач. Здесь живёт весь SQL.

Модуль сознательно не знает про FastAPI: возвращает данные и поднимает
TaskError, а как это превратить в HTTP — дело слоя routes. Поэтому один и тот
же код обслуживает и JSON-API, и дэшборд, и тесты без поднятия приложения.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from api.models import database
from api.models.envelope import Outcome
from api.models.task import MAX_LIMIT, ActorId, Status, Task, TaskSummary


class TaskError(Exception):
    """Ошибка, понятная вызывающему слою: code — это значение Outcome."""

    def __init__(self, code: Outcome, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _actor(value: ActorId | None, field: str) -> str | None:
    """assignee_id и created_by приходят как UUID, строка или int — храним TEXT."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TaskError(Outcome.validation_error, f"{field} должен быть строкой или числом")
    out = str(value).strip()
    if not out:
        raise TaskError(Outcome.validation_error, f"{field} не может быть пустым (для «без исполнителя» передайте null)")
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


def _fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task": json.loads(row["task"]),
        "status": row["status"],
        "project": row["project"],
        "assignee_id": row["assignee_id"],
        "created_by": row["created_by"],
        "parent_id": row["parent_id"],
        "key": row["key"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _task(row: sqlite3.Row) -> Task:
    return Task(**_fields(row), result=json.loads(row["result"]) if row["result"] is not None else None)


def _summary(row: sqlite3.Row) -> TaskSummary:
    return TaskSummary(**_fields(row))


def _fetch(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def create(
    task: Any,
    project: str | None = None,
    assignee_id: ActorId | None = None,
    parent_id: int | None = None,
    key: str | None = None,
    created_by: ActorId | None = None,
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

    now = time.time()
    with database.transaction() as conn:
        if parent_id is not None and _fetch(conn, parent_id) is None:
            raise TaskError(Outcome.parent_not_found, f"родительской задачи {parent_id} не существует")
        cur = conn.execute(
            """
            INSERT INTO tasks (task, status, project, assignee_id, created_by, parent_id, key, created_at, updated_at)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (payload, scope, assignee, creator, parent_id, key, now, now),
        )
        if cur.rowcount == 0:
            existing = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            return _task(existing), False
        return _task(_fetch(conn, cur.lastrowid)), True


def get(task_id: int) -> Task | None:
    with database.reading() as conn:
        row = _fetch(conn, task_id)
    return _task(row) if row is not None else None


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

    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), MAX_LIMIT)))

    with database.reading() as conn:
        return [_summary(r) for r in conn.execute(sql, args)]


def set_status(
    task_id: int,
    status: Status | str,
    result: Any = None,
    if_status: Status | str | None = None,
) -> tuple[Outcome, Task | None]:
    """Сменить статус, при необходимости приложив результат.

    if_status делает вызов compare-and-set: запись проходит только если задача
    всё ещё в этом статусе. Так двое не доделывают одну задачу.
    """
    new_status = _status(status)
    expected = _status(if_status, "if_status") if if_status is not None else None
    payload = _dump(result, "result") if result is not None else None

    with database.transaction() as conn:
        row = _fetch(conn, task_id)
        if row is None:
            return Outcome.not_found, None
        if expected is not None and row["status"] != expected.value:
            return Outcome.status_conflict, _task(row)
        conn.execute(
            "UPDATE tasks SET status = ?, result = COALESCE(?, result), updated_at = ? WHERE id = ?",
            (new_status.value, payload, time.time(), task_id),
        )
        return Outcome.updated, _task(_fetch(conn, task_id))


def claim(assignee_id: ActorId, project: str | None = None) -> Task | None:
    """Атомарно забрать одну pending-задачу и перевести её в in_progress.

    Сначала адресованные этому исполнителю, потом общий пул, внутри группы FIFO.
    Select и update идут в одной транзакции под локом, поэтому двое не могут
    забрать одну строку.

    `project` сужает выборку строго: агент, назвавший проект, не заберёт ни чужую
    задачу, ни задачу без проекта. Не назвал — берёт откуда угодно.
    """
    assignee = _actor(assignee_id, "assignee_id")
    if assignee is None:
        raise TaskError(Outcome.validation_error, "assignee_id обязателен для захвата задачи")
    scope = _actor(project, "project")

    with database.transaction() as conn:
        row = conn.execute(
            """
            SELECT id FROM tasks
             WHERE status = 'pending'
               AND (assignee_id IS NULL OR assignee_id = :me)
               AND (:scope IS NULL OR project = :scope)
             ORDER BY (assignee_id IS NULL), id
             LIMIT 1
            """,
            {"me": assignee, "scope": scope},
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tasks SET status = 'in_progress', assignee_id = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (assignee, time.time(), row["id"]),
        )
        if cur.rowcount == 0:
            return None
        return _task(_fetch(conn, row["id"]))


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
