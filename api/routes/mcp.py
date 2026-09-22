"""MCP поверх HTTP — тот же порт, что и у API.

Транспорт streamable-http вместо stdio: ядро уже слушает порт, поэтому агенту
не нужен отдельный процесс-адаптер. Инструменты зовут те же сервисы, что и
JSON-роуты, а не ходят по HTTP в самих себя.

stdio-адаптер остаётся в `mcp_adapter/` для клиентов, которые не умеют HTTP.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from api.models.envelope import Outcome, body, envelope
from api.models.task import MAX_TIMEOUT_S, Status
from api.services import tasks as tasks_service
from api.services.tasks import TaskError
from api.utils import events, run_service
from api.utils.tool_docs import (
    CLAIM_TASK,
    GET_TASK,
    GET_TASKS,
    HARD_ERRORS,
    HEARTBEAT,
    SET_STATUS,
    SET_TASK,
)

INSTRUCTIONS = (
    "Таск-менеджер для агентов. Ставьте задачи через set_task, забирайте работу через "
    "claim_task (timeout_s>0 — ждать появления задачи), отчитывайтесь через set_status. "
    "Каждый ответ содержит поле outcome — по нему судите об исходе операции."
)


def _out(env) -> dict[str, Any]:
    """Конверт наружу. Жёсткие ошибки поднимаются как ошибка инструмента."""
    payload = body(env)
    if payload.get("outcome") in HARD_ERRORS:
        raise ToolError(f"{payload['outcome']}: {payload.get('message') or 'ошибка вызова'}")
    return payload


async def set_task(
    task: dict[str, Any],
    project: str | None = None,
    assignee_id: str | int | None = None,
    parent_id: int | None = None,
    key: str | None = None,
    created_by: str | int | None = None,
    priority: int = 0,
    max_attempts: int | None = None,
    depends_on: list[int] | None = None,
) -> dict[str, Any]:
    try:
        created_task, created = await run_service(
            tasks_service.create,
            task=task,
            project=project,
            assignee_id=assignee_id,
            parent_id=parent_id,
            key=key,
            created_by=created_by,
            priority=priority,
            max_attempts=max_attempts,
            depends_on=depends_on,
        )
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message, task=None))

    if created:
        await events.notify_new_task()
    return _out(envelope(Outcome.created if created else Outcome.exists, task=created_task))


async def get_tasks(
    assignee_id: str | int | None = None,
    status: str | list[str] | None = None,
    project: str | None = None,
    unscoped: bool = False,
    unassigned: bool = False,
    parent_id: int | None = None,
    stale_seconds: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    try:
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
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message, tasks=[], count=0))
    return _out(envelope(Outcome.ok, tasks=found, count=len(found)))


async def get_task(task_id: int, with_events: bool = False) -> dict[str, Any]:
    found = await run_service(tasks_service.get, task_id)
    if found is None:
        return _out(envelope(Outcome.not_found, f"задачи {task_id} не существует", task=None))
    if not with_events:
        return _out(envelope(Outcome.ok, task=found))
    log = await run_service(tasks_service.events, task_id)
    return _out(envelope(Outcome.ok, task=found, events=log, count=len(log)))


async def set_status(
    task_id: int,
    status: str,
    result: Any = None,
    if_status: str | None = None,
    assignee_id: str | int | None = None,
) -> dict[str, Any]:
    try:
        outcome, task = await run_service(
            tasks_service.set_status,
            task_id=task_id,
            status=status,
            result=result,
            if_status=if_status,
            actor=assignee_id,
        )
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message, task=None))

    if outcome is Outcome.updated:
        await (events.notify_new_task() if status == Status.pending.value else events.notify_change())

    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: f"ожидался статус {if_status}, а задача уже в "
        f"{task.status.value if task else ''}",
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
    }.get(outcome)
    return _out(envelope(outcome, message, task=task))


async def claim_task(
    assignee_id: str | int,
    project: str | None = None,
    timeout_s: float = 0,
    lease_s: float | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(max(timeout_s, 0.0), MAX_TIMEOUT_S)
    while True:
        try:
            task = await run_service(tasks_service.claim, assignee_id, project=project, lease_s=lease_s)
        except TaskError as exc:
            return _out(envelope(exc.code, exc.message, task=None))

        if task is not None:
            await events.notify_change()
            return _out(envelope(Outcome.claimed, task=task))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _out(envelope(Outcome.empty, task=None))
        await events.wait_for_task(remaining)


async def heartbeat(
    task_id: int,
    assignee_id: str | int,
    lease_s: float | None = None,
) -> dict[str, Any]:
    outcome, task = await run_service(
        tasks_service.heartbeat, task_id=task_id, assignee_id=assignee_id, lease_s=lease_s
    )
    message = {
        Outcome.not_found: f"задачи {task_id} не существует",
        Outcome.status_conflict: f"задача не в работе, а в {task.status.value if task else ''}",
        Outcome.not_owner: f"задача занята исполнителем {task.assignee_id if task else ''}",
    }.get(outcome)
    return _out(envelope(outcome, message, task=task))


#: Менеджер сессий транспорта можно запустить только один раз на экземпляр,
#: поэтому сервер собирается фабрикой: у каждого приложения — свой.
def build() -> MCPServer:
    server = MCPServer(name="noted", instructions=INSTRUCTIONS)
    server.tool(description=SET_TASK)(set_task)
    server.tool(description=GET_TASKS)(get_tasks)
    server.tool(description=GET_TASK)(get_task)
    server.tool(description=SET_STATUS)(set_status)
    server.tool(description=CLAIM_TASK)(claim_task)
    server.tool(description=HEARTBEAT)(heartbeat)
    return server
