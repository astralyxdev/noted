"""JSON-API домена задач. Только HTTP: разобрать, позвать сервис, упаковать."""

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
    """Забрать задачу. timeout_s > 0 — long-poll: ждём появления новой,
    вместо того чтобы заставлять агента крутить опрос."""
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
async def set_task_status(task_id: int, request: SetStatusRequest):
    outcome, task = await run_service(
        tasks_service.set_status,
        task_id=task_id,
        status=request.status,
        result=request.result,
        if_status=request.if_status,
        actor=request.assignee_id,
    )
    if outcome is Outcome.updated:
        # Возврат в pending — это новая работа для агентов; остальное видит только дэшборд.
        await (events.notify_new_task() if request.status is Status.pending else events.notify_change())

    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: f"ожидался статус {request.if_status.value if request.if_status else ''}, "
        f"а задача уже в {task.status.value if task else ''}",
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat(task_id: int, request: HeartbeatRequest):
    """Продлить аренду: исполнитель жив и всё ещё делает эту задачу."""
    outcome, task = await run_service(
        tasks_service.heartbeat,
        task_id=task_id,
        assignee_id=request.assignee_id,
        lease_s=request.lease_s,
    )
    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: f"задача не в работе, а в {task.status.value if task else ''}",
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
    }.get(outcome)
    return respond(envelope(outcome, message, task=task))


@router.get("/tasks/{task_id}/events")
async def get_task_events(task_id: int, limit: int = 200):
    """Журнал переходов задачи — кто и когда что с ней сделал."""
    task = await run_service(tasks_service.get, task_id)
    if task is None:
        return respond(envelope(Outcome.not_found, f"задачи {task_id} не существует", task=None))
    log = await run_service(tasks_service.events, task_id, limit)
    return respond(envelope(Outcome.ok, task=task, events=log, count=len(log)))


@router.get("/stats")
async def get_stats(project: str | None = None, unscoped: bool = False):
    """Всё, что нужно шапке дэшборда, одним запросом: счётчики внутри скоупа
    плюс списки проектов и исполнителей для фильтров."""
    counts = await run_service(tasks_service.stats, project=project, unscoped=unscoped)
    known_projects = await run_service(tasks_service.projects)
    known_assignees = await run_service(tasks_service.assignees)
    return respond(envelope(Outcome.ok, stats=counts, projects=known_projects, assignees=known_assignees))
