"""Task domain schemas: the task itself and the request bodies."""

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

#: How long a task stays with its executor when no lease was asked for.
DEFAULT_LEASE_S = 300.0
MAX_LEASE_S = 86_400.0


class TaskSummary(BaseModel):
    """List view: no `result`, so a page of tasks cannot burn an agent's context."""

    id: int
    task: dict[str, Any]
    status: Status
    project: str | None
    assignee_id: str | None
    created_by: str | None
    parent_id: int | None
    key: str | None
    priority: int
    attempts: int
    #: How many predecessors are still open. While this is above zero the task
    #: cannot be claimed, even though it sits in pending.
    waiting_on: int = 0
    max_attempts: int | None
    retry_after: str | None
    lease_expires: str | None
    created_at: str
    updated_at: str


class Task(TaskSummary):
    result: Any = None
    depends_on: list[int] = []


class TaskEvent(BaseModel):
    """A journal entry. Append-only; entries are never rewritten."""

    id: int
    task_id: int
    at: str
    event: str
    actor: str | None
    from_status: Status | None
    to_status: Status | None
    detail: Any = None


class CreateTaskRequest(BaseModel):
    task: dict[str, Any]
    project: str | None = None
    assignee_id: ActorId | None = None
    parent_id: int | None = None
    key: str | None = None
    created_by: ActorId | None = None
    priority: int = 0
    max_attempts: int | None = Field(default=None, ge=1)
    depends_on: list[int] | None = None


class SetStatusRequest(BaseModel):
    status: Status
    result: Any = None
    if_status: Status | None = None
    #: Who is writing. A task held by someone else answers with not_owner.
    assignee_id: ActorId | None = None
    #: Unconditional write. Compare-and-set is the default on purpose: a
    #: forgotten if_status must not break invariants in silence.
    force: bool = False


class ClaimRequest(BaseModel):
    assignee_id: ActorId
    project: str | None = None
    timeout_s: float = Field(default=0.0, ge=0.0)
    #: Lease length. Null means no lease: the task stays with its holder.
    lease_s: float | None = Field(default=None, ge=0.0)


class HeartbeatRequest(BaseModel):
    assignee_id: ActorId
    lease_s: float | None = Field(default=None, ge=0.0)
