"""The JSON API of the task domain. HTTP only: parse, call a service, wrap."""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Query, Request

from api.models.envelope import Outcome, envelope
from api.models.task import (
    MAX_TIMEOUT_S,
    LoginRequest,
    ClaimRequest,
    CreateTaskRequest,
    HeartbeatRequest,
    SetStatusRequest,
    Status,
)
from api.services import agents as agents_service
from api.services import tasks as tasks_service
from api.utils import events, respond, run_service
from api.utils import authorization
from api.utils.authorization import of as principal_of

router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/tasks")
async def create_task(request: CreateTaskRequest, http: Request):
    who = principal_of(http)
    if who.agent and not who.agent.may_touch(request.project):
        return respond(envelope(Outcome.forbidden, f"project {request.project} is outside the key's scope", task=None))
    task, created = await run_service(
        tasks_service.create,
        task=request.task,
        project=request.project,
        assignee_id=request.assignee_id,
        parent_id=request.parent_id,
        key=request.key,
        # Authorship cannot be forged: for an agent it follows from the key.
        created_by=who.name or request.created_by,
        priority=request.priority,
        max_attempts=request.max_attempts,
        depends_on=request.depends_on,
        allowed_projects=who.projects,
    )
    if created:
        await events.notify_new_task()
    return respond(envelope(Outcome.created if created else Outcome.exists, task=task))


@router.post("/tasks/claim")
async def claim_task(request: ClaimRequest, http: Request):
    """Take a task. timeout_s > 0 is a long poll: the request waits for work
    to appear instead of making the agent spin on polling."""
    who = principal_of(http)
    deadline = time.monotonic() + min(request.timeout_s, MAX_TIMEOUT_S)
    while True:
        task = await run_service(
            tasks_service.claim,
            who.name or request.assignee_id,
            project=request.project,
            lease_s=request.lease_s,
            session_id=who.session_id,
            allowed_projects=who.projects,
        )
        if task is not None:
            await events.notify_change()
            return respond(envelope(Outcome.claimed, task=task))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return respond(envelope(Outcome.empty, task=None))
        await events.wait_for_task(remaining)


@router.get("/tasks")
async def list_tasks(
    http: Request,
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
        allowed_projects=principal_of(http).projects,
        limit=limit,
    )
    return respond(envelope(Outcome.ok, tasks=found, count=len(found)))


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, http: Request):
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"task {task_id} does not exist", task=None))
    who = principal_of(http)
    if not who.may_touch(task.project):
        return respond(envelope(Outcome.forbidden, f"project {task.project} is outside the key's scope", task=None))
    return respond(envelope(Outcome.ok, task=task))


@router.patch("/tasks/{task_id}/status")
async def set_task_status(task_id: int, request: SetStatusRequest, http: Request):
    who = principal_of(http)
    # force is a human overriding the queue, not an agent that forgot if_status.
    if request.force and who.agent is not None and not who.is_admin:
        return respond(envelope(Outcome.forbidden, "force is for administrators", task=None))

    existing = await run_service(tasks_service.get, task_id)
    if existing is not None and not who.may_touch(existing.project):
        return respond(envelope(Outcome.forbidden, f"project {existing.project} is outside the key's scope", task=None))
    outcome, task = await run_service(
        tasks_service.set_status,
        task_id=task_id,
        status=request.status,
        result=request.result,
        if_status=request.if_status,
        actor=who.name or request.assignee_id,
        session_id=who.session_id,
        strict_session=who.agent is not None and not who.is_admin,
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
        Outcome.stale_session: "another instance of this agent holds the task",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat(task_id: int, request: HeartbeatRequest, http: Request):
    """Extend the lease: the executor is alive and still on this task."""
    who = principal_of(http)
    # The answer carries the whole task, result included, so this is a read
    # before it is a write and the scope has to be checked as one.
    existing = await run_service(tasks_service.get, task_id)
    if existing is not None and not who.may_touch(existing.project):
        return respond(envelope(Outcome.forbidden, f"project {existing.project} is outside the key's scope", task=None))
    outcome, task = await run_service(
        tasks_service.heartbeat,
        task_id=task_id,
        assignee_id=who.name or request.assignee_id,
        lease_s=request.lease_s,
        session_id=who.session_id,
        strict_session=who.agent is not None and not who.is_admin,
    )
    message = {
        Outcome.not_found: f"task {task_id} does not exist",
        Outcome.status_conflict: f"the task is not in progress but {task.status.value if task else ''}",
        Outcome.not_owner: f"the task is held by {task.assignee_id if task else ''}",
        Outcome.stale_session: "another instance of this agent holds the task",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.get("/tasks/{task_id}/events")
async def get_task_events(task_id: int, http: Request, limit: int = 200):
    """The transition journal of a task: who did what to it, and when."""
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"task {task_id} does not exist", task=None))
    if not principal_of(http).may_touch(task.project):
        return respond(envelope(Outcome.forbidden, f"project {task.project} is outside the key's scope", task=None))
    log = await run_service(tasks_service.events, task_id, limit)
    return respond(envelope(Outcome.ok, task=task, events=log, count=len(log)))


@router.post("/login")
async def login(request: LoginRequest, http: Request):
    """The dashboard door. A browser sends no headers, so it gets a cookie."""
    presented = request.token.strip()
    expected = authorization.admin_token()

    # Either the shared admin token, or an admin key issued by noted-keys:
    # without the second, a queue with identity on and no NOTED_TOKEN would have
    # no way in at all.
    by_token = bool(expected) and hmac.compare_digest(presented.encode(), expected.encode())
    agent = await run_service(agents_service.resolve, presented)
    by_key = agent is not None and agent.is_admin

    if not expected and not agents_service.identity_required():
        return respond(envelope(Outcome.ok, "no token is set, so no login is needed"))
    if not (by_token or by_key):
        return respond(envelope(Outcome.unauthorized, "wrong token"))

    response = respond(envelope(Outcome.ok))
    response.set_cookie(
        authorization.COOKIE,
        authorization.open_browser_pass(),
        max_age=authorization.COOKIE_TTL_S,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(http: Request):
    authorization.close_browser_pass(http.cookies.get(authorization.COOKIE))
    response = respond(envelope(Outcome.ok))
    response.delete_cookie(authorization.COOKIE)
    return response


@router.post("/sessions/renew")
async def renew_session(http: Request):
    """Proof of life from the transport rather than from the model.

    A model cannot renew anything while a ten-minute build runs: between tool
    calls it holds no control. The adapter can — it is a separate process and
    lives for as long as its client does.
    """
    who = principal_of(http)
    if not who.session_id:
        return respond(envelope(Outcome.validation_error, "a session id header is required"))
    await run_service(agents_service.renew_session, who.session_id)
    return respond(envelope(Outcome.ok))


@router.get("/stats")
async def get_stats(http: Request, project: str | None = None, unscoped: bool = False):
    """Everything the dashboard header needs in one request: counters inside
    the current scope, plus the project and assignee lists for the filters.

    All three are readable data, so all three follow the key's scope — the
    counters, the project names and the assignee names alike."""
    allowed = principal_of(http).projects
    counts = await run_service(tasks_service.stats, project=project, unscoped=unscoped, allowed_projects=allowed)
    known_projects = await run_service(tasks_service.projects, allowed_projects=allowed)
    known_assignees = await run_service(tasks_service.assignees, allowed_projects=allowed)
    return respond(envelope(Outcome.ok, stats=counts, projects=known_projects, assignees=known_assignees))
