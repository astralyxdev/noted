"""Схемы домена задач: сама задача и тела запросов."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

ActorId = str | int


class Status(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL = frozenset({Status.done, Status.failed, Status.cancelled})
OPEN = tuple(s for s in Status if s not in TERMINAL)

MAX_LIMIT = 500
MAX_TIMEOUT_S = 300.0


class TaskSummary(BaseModel):
    """Вид для списка: без `result`, чтобы выдача не сжигала контекст агента."""

    id: int
    task: dict[str, Any]
    status: Status
    project: str | None
    assignee_id: str | None
    created_by: str | None
    parent_id: int | None
    key: str | None
    created_at: str
    updated_at: str


class Task(TaskSummary):
    result: Any = None


class CreateTaskRequest(BaseModel):
    task: dict[str, Any]
    project: str | None = None
    assignee_id: ActorId | None = None
    parent_id: int | None = None
    key: str | None = None
    created_by: ActorId | None = None


class SetStatusRequest(BaseModel):
    status: Status
    result: Any = None
    if_status: Status | None = None


class ClaimRequest(BaseModel):
    assignee_id: ActorId
    project: str | None = None
    timeout_s: float = Field(default=0.0, ge=0.0)
