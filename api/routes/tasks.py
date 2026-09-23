"""The JSON API of the task domain. HTTP only: parse, call a service, wrap."""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from api.models.envelope import Outcome, envelope
from api.models.task import (
    MAX_TIMEOUT_S,
    ClaimRequest,
    CreateTaskRequest,
    HeartbeatRequest,
    SetStatusRequest,
    Status,
)
from api.services import tasks as tasks_service
from api.utils import events, respond, run_service

#: The longest a long poll sleeps before trying again, however long the caller
#: asked to wait for. See the note in claim_task.
POLL_CEILING_S = 15.0

router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/tasks")
async def create_task(request: CreateTaskRequest):
    task, created = await run_service(
        tasks_service.create,
        task=request.task,
        project=request.project,
        assignee_id=request.assignee_id,
        parent_id=request.parent_id,
        key=request.key,
        created_by=request.created_by,
        priority=request.priority,
        max_attempts=request.max_attempts,
        depends_on=request.depends_on,
    )
    if created:
        await events.notify_new_task()
    return respond(envelope(Outcome.created if created else Outcome.exists, task=task))


@router.post("/tasks/claim")
async def claim_task(request: ClaimRequest):
    """Take a task. timeout_s > 0 is a long poll: the request waits for work
    to appear instead of making the agent spin on polling."""
    deadline = time.monotonic() + min(request.timeout_s, MAX_TIMEOUT_S)
    while True:
        task = await run_service(
            tasks_service.claim,
            request.assignee_id,
            project=request.project,
            lease_s=request.lease_s,
        )
        if task is not None:
            await events.notify_change()
            return respond(envelope(Outcome.claimed, task=task))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return respond(envelope(Outcome.empty, task=None))
        # Capped rather than waiting out the whole timeout: the waiter only
        # registers on the condition after the claim above has returned, so a
        # notification fired in that gap is missed. The window is tiny, and
        # this bounds what it can cost to one short wait instead of minutes.
        await events.wait_for_task(min(remaining, POLL_CEILING_S))


@router.get("/tasks")
async def list_tasks(
    assignee_id: str | None = None,
    status: list[Status] | None = Query(default=None),
    project: str | None = None,
    unscoped: bool = False,
    unassigned: bool = False,
    parent_id: int | None = None,
    stale_seconds: float | None = None,
    before_id: int | None = None,
    limit: int = 50,
):
    found = await run_service(
        tasks_service.list_tasks,
        assignee_id=assignee_id,
        status=status,
        project=project,
        unscoped=unscoped,
        unassigned=unassigned,
        parent_id=parent_id,
        stale_seconds=stale_seconds,
        before_id=before_id,
        limit=limit,
    )
    return respond(envelope(Outcome.ok, tasks=found, count=len(found)))


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"task {task_id} does not exist", task=None))
    return respond(envelope(Outcome.ok, task=task))


@router.patch("/tasks/{task_id}/status")
async def set_task_status(task_id: int, request: SetStatusRequest):
    outcome, task = await run_service(
        tasks_service.set_status,
        task_id=task_id,
        status=request.status,
        result=request.result,
        if_status=request.if_status,
        actor=request.assignee_id,
        force=request.force,
    )
    if outcome is Outcome.updated:
        # Work may have appeared for a waiting agent in two ways: this task is
        # claimable again — moved back to pending, or sent back by a retry —
        # or it closed successfully and released whatever depended on it.
        # Judged by the status the task ended up in, not the one that was
        # asked for: a retry is requested as `failed` and lands on `pending`.
        freed = task is not None and task.status in (Status.pending, Status.done)
        await (events.notify_new_task() if freed else events.notify_change())

    message = {
        Outcome.not_found: f"task {task_id} does not exist",
        Outcome.status_conflict: (
            f"expected status {request.if_status.value if request.if_status else 'in_progress'}, "
            f"but the task is {task.status.value if task else ''}; "
            "an unconditional write is force=true"
        ),
        Outcome.not_owner: f"the task is held by {task.assignee_id if task else ''}",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat(task_id: int, request: HeartbeatRequest):
    """Extend the lease: the executor is alive and still on this task."""
    outcome, task = await run_service(
        tasks_service.heartbeat,
        task_id=task_id,
        assignee_id=request.assignee_id,
        lease_s=request.lease_s,
    )
    message = {
        Outcome.not_found: f"task {task_id} does not exist",
        Outcome.status_conflict: f"the task is not in progress but {task.status.value if task else ''}",
        Outcome.not_owner: f"the task is held by {task.assignee_id if task else ''}",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.get("/tasks/{task_id}/events")
async def get_task_events(task_id: int, limit: int = 200):
    """The transition journal of a task: who did what to it, and when."""
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"task {task_id} does not exist", task=None))
    log = await run_service(tasks_service.events, task_id, limit)
    return respond(envelope(Outcome.ok, task=task, events=log, count=len(log)))


@router.get("/stats")
async def get_stats(project: str | None = None, unscoped: bool = False):
    """Everything the dashboard header needs in one request: counters inside
    the current filter, plus the project and assignee lists for the filters."""
    counts = await run_service(tasks_service.stats, project=project, unscoped=unscoped)
    known_projects = await run_service(tasks_service.projects)
    known_assignees = await run_service(tasks_service.assignees)
    return respond(envelope(Outcome.ok, stats=counts, projects=known_projects, assignees=known_assignees))
