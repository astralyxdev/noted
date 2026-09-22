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
        return respond(envelope(Outcome.forbidden, f"проект {request.project} вне скоупа ключа", task=None))
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
        return respond(envelope(Outcome.not_found, f"задачи {task_id} не существует", task=None))
    return respond(envelope(Outcome.ok, task=task))


@router.patch("/tasks/{task_id}/status")
async def set_task_status(task_id: int, request: SetStatusRequest, http: Request):
    who = principal_of(http)
    outcome, task = await run_service(
        tasks_service.set_status,
        task_id=task_id,
        status=request.status,
        result=request.result,
        if_status=request.if_status,
        actor=who.name or request.assignee_id,
        session_id=who.session_id,
        force=request.force,
    )
    if outcome is Outcome.updated:
        # Back to pending means new work for agents; the rest only the dashboard sees.
        await (events.notify_new_task() if request.status is Status.pending else events.notify_change())

    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: (
            f"ожидался статус {request.if_status.value if request.if_status else 'in_progress'}, "
            f"а задача в {task.status.value if task else ''}; "
            "безусловная запись — это force=true"
        ),
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
        Outcome.stale_session: "задачу держит другой экземпляр этого агента",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat(task_id: int, request: HeartbeatRequest, http: Request):
    who = principal_of(http)
    """Extend the lease: the executor is alive and still on this task."""
    outcome, task = await run_service(
        tasks_service.heartbeat,
        task_id=task_id,
        assignee_id=who.name or request.assignee_id,
        lease_s=request.lease_s,
        session_id=who.session_id,
    )
    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: f"задача не в работе, а в {task.status.value if task else ''}",
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
        Outcome.stale_session: "задачу держит другой экземпляр этого агента",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.get("/tasks/{task_id}/events")
async def get_task_events(task_id: int, limit: int = 200):
    """The transition journal of a task: who did what to it, and when."""
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"задачи {task_id} не существует", task=None))
    log = await run_service(tasks_service.events, task_id, limit)
    return respond(envelope(Outcome.ok, task=task, events=log, count=len(log)))


@router.post("/login")
async def login(request: LoginRequest, http: Request):
    """The dashboard door. A browser sends no headers, so it gets a cookie."""
    expected = authorization.admin_token()
    if not expected:
        return respond(envelope(Outcome.ok, "токен не задан — вход не требуется"))
    # Compared as bytes: compare_digest refuses non-ASCII strings.
    if not hmac.compare_digest(request.token.strip().encode(), expected.encode()):
        return respond(envelope(Outcome.unauthorized, "неверный токен"))

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
        return respond(envelope(Outcome.validation_error, f"нужен заголовок с идентификатором сессии"))
    await run_service(agents_service.renew_session, who.session_id)
    return respond(envelope(Outcome.ok))


@router.get("/stats")
async def get_stats(project: str | None = None, unscoped: bool = False):
    """Everything the dashboard header needs in one request: counters inside
    the current scope, plus the project and assignee lists for the filters."""
    counts = await run_service(tasks_service.stats, project=project, unscoped=unscoped)
    known_projects = await run_service(tasks_service.projects)
    known_assignees = await run_service(tasks_service.assignees)
    return respond(envelope(Outcome.ok, stats=counts, projects=known_projects, assignees=known_assignees))
