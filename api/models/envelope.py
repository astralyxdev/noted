"""Единый конверт ответа.

Лежит в models, потому что им пользуются оба верхних слоя и MCP-адаптер:
так форма ответа не разъезжается между JSON-роутами и инструментами агента.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from api.models.task import Task, TaskSummary


class Outcome(str, Enum):
    created = "created"
    exists = "exists"
    ok = "ok"
    updated = "updated"
    claimed = "claimed"
    empty = "empty"
    not_found = "not_found"
    status_conflict = "status_conflict"
    parent_not_found = "parent_not_found"
    unauthorized = "unauthorized"
    validation_error = "validation_error"
    api_unavailable = "api_unavailable"
    internal_error = "internal_error"


HTTP_STATUS: dict[Outcome, int] = {
    Outcome.created: 201,
    Outcome.exists: 200,
    Outcome.ok: 200,
    Outcome.updated: 200,
    Outcome.claimed: 200,
    Outcome.empty: 200,
    Outcome.not_found: 404,
    Outcome.status_conflict: 409,
    Outcome.parent_not_found: 404,
    Outcome.unauthorized: 401,
    Outcome.validation_error: 422,
    Outcome.api_unavailable: 503,
    Outcome.internal_error: 500,
}

SUCCESS = frozenset(
    {Outcome.created, Outcome.exists, Outcome.ok, Outcome.updated, Outcome.claimed, Outcome.empty}
)

#: Исходы, которые агент должен получить как ошибку инструмента, а не как результат.
HARD_ERRORS = frozenset(
    {Outcome.unauthorized, Outcome.validation_error, Outcome.api_unavailable, Outcome.internal_error}
)


class Envelope(BaseModel):
    ok: bool
    outcome: Outcome
    message: str | None = None
    task: Task | TaskSummary | None = None
    tasks: list[TaskSummary] | None = None
    count: int | None = None
    stats: dict[str, int] | None = None
    projects: list[str] | None = None
    assignees: list[str] | None = None


def envelope(outcome: Outcome, message: str | None = None, **payload: Any) -> Envelope:
    return Envelope(ok=outcome in SUCCESS, outcome=outcome, message=message, **payload)


def body(env: Envelope) -> dict[str, Any]:
    """Сериализация с exclude_unset: `task: null` остаётся, если его передали явно,
    а незаполненные `tasks`/`count` в ответ не попадают."""
    return env.model_dump(mode="json", exclude_unset=True)
