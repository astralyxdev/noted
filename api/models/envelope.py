"""The single response envelope.

It lives in models because every delivery layer uses it — JSON routes, MCP
tools — so the shape of an answer cannot drift apart
between them.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from api.models.task import Task, TaskEvent, TaskSummary


class Outcome(str, Enum):
    created = "created"
    exists = "exists"
    ok = "ok"
    updated = "updated"
    claimed = "claimed"
    empty = "empty"
    not_found = "not_found"
    status_conflict = "status_conflict"
    not_owner = "not_owner"
    rate_limited = "rate_limited"
    parent_not_found = "parent_not_found"
    validation_error = "validation_error"
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
    Outcome.not_owner: 409,
    Outcome.rate_limited: 429,
    Outcome.parent_not_found: 404,
    Outcome.validation_error: 422,
    Outcome.internal_error: 500,
}

SUCCESS = frozenset(
    {Outcome.created, Outcome.exists, Outcome.ok, Outcome.updated, Outcome.claimed, Outcome.empty}
)

#: Outcomes an agent must receive as a tool error rather than as a result.
HARD_ERRORS = frozenset(
    {
        Outcome.validation_error,
        Outcome.internal_error,
        # A flooding agent needs an exception, not a quiet "no": otherwise the
        # loop that got it there keeps spinning.
        Outcome.rate_limited,
    }
)


class Envelope(BaseModel):
    ok: bool
    outcome: Outcome
    message: str | None = None
    task: Task | TaskSummary | None = None
    tasks: list[TaskSummary] | None = None
    events: list[TaskEvent] | None = None
    requeued: list[int] | None = None
    count: int | None = None
    stats: dict[str, int] | None = None
    projects: list[str] | None = None
    assignees: list[str] | None = None


def envelope(outcome: Outcome, message: str | None = None, **payload: Any) -> Envelope:
    return Envelope(ok=outcome in SUCCESS, outcome=outcome, message=message, **payload)


def body(env: Envelope) -> dict[str, Any]:
    """Serialised with exclude_unset: an explicit `task: null` survives, while
    fields nobody filled in stay out of the response."""
    return env.model_dump(mode="json", exclude_unset=True)
