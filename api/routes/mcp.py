"""MCP over HTTP — on the same port as the API, at /mcp.

The only transport. The core already listens on a port, so an agent needs no
adapter process of its own: it points a streamable-http MCP client at
http://host:port/mcp/ and that is the whole setup. The tools call the same
services the JSON routes do rather than looping back over HTTP.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from api.models.envelope import Outcome, body, envelope
from api.models.task import MAX_TIMEOUT_S, Status
from api.routes.tasks import POLL_CEILING_S
from api.services import tasks as tasks_service
from api.services.tasks import TaskError
from api.utils import events, run_service
from api.utils.authorization import ANONYMOUS, Principal
from api.utils.authorization import principal_from, transport_of
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
    "A task manager for agents. Post work with set_task, take work with claim_task "
    "(timeout_s>0 waits for a task to appear), report back with set_status. "
    "Every answer carries an `outcome` field — judge the result by it."
)


def _who(ctx: Context | None) -> Principal:
    """Who is calling the tool. The middleware has already checked the headers
    and renewed the session for this request; here we only read identity out of
    them, so it never comes from the arguments — and renewing a second time
    would be a second write under the global lock for one call."""
    if ctx is None:
        return ANONYMOUS
    return principal_from(ctx.headers, transport_of(ctx.headers, "/mcp"), renew=False) or ANONYMOUS


def _out(env) -> dict[str, Any]:
    """The envelope on its way out. Hard errors are raised as tool errors."""
    payload = body(env)
    if payload.get("outcome") in HARD_ERRORS:
        raise ToolError(f"{payload['outcome']}: {payload.get('message') or 'call failed'}")
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    who = _who(ctx)
    if who.agent and not who.agent.may_touch(project):
        return _out(envelope(Outcome.forbidden, f"project {project} is outside the key's scope", task=None))
    try:
        created_task, created = await run_service(
            tasks_service.create,
            task=task,
            project=project,
            assignee_id=assignee_id,
            parent_id=parent_id,
            key=key,
            created_by=who.name or created_by,
            priority=priority,
            max_attempts=max_attempts,
            depends_on=depends_on,
            allowed_projects=who.projects,
        )
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message))

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
    before_id: int | None = None,
    limit: int = 50,
    ctx: Context | None = None,
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
            before_id=before_id,
            allowed_projects=_who(ctx).projects,
            limit=limit,
        )
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message))
    return _out(envelope(Outcome.ok, tasks=found, count=len(found)))


async def get_task(task_id: int, with_events: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    try:
        found = await run_service(tasks_service.get, task_id)
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message))
    if found is None:
        return _out(envelope(Outcome.not_found, f"task {task_id} does not exist", task=None))
    if not _who(ctx).may_touch(found.project):
        return _out(envelope(Outcome.forbidden, f"project {found.project} is outside the key's scope", task=None))
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
    force: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    who = _who(ctx)
    if force and who.agent is not None and not who.is_admin:
        return _out(envelope(Outcome.forbidden, "force is for administrators", task=None))

    existing = await run_service(tasks_service.get, task_id)
    if existing is not None and not who.may_touch(existing.project):
        return _out(envelope(Outcome.forbidden, f"project {existing.project} is outside the key's scope", task=None))

    try:
        outcome, task = await run_service(
            tasks_service.set_status,
            task_id=task_id,
            status=status,
            result=result,
            if_status=if_status,
            actor=who.name or assignee_id,
            session_id=who.session_id,
            strict_session=who.agent is not None and not who.is_admin,
            force=force,
        )
    except TaskError as exc:
        return _out(envelope(exc.code, exc.message))

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
            f"expected status {if_status or 'in_progress'}, but the task is "
            f"{task.status.value if task else ''}; an unconditional write is force=true"
        ),
        Outcome.not_owner: f"the task is held by {task.assignee_id if task else ''}",
        Outcome.stale_session: "another instance of this agent holds the task",
    }.get(outcome)
    return _out(envelope(outcome, message, task=task))


async def claim_task(
    assignee_id: str | int,
    project: str | None = None,
    timeout_s: float = 0,
    lease_s: float | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    who = _who(ctx)
    deadline = time.monotonic() + min(max(timeout_s, 0.0), MAX_TIMEOUT_S)
    while True:
        try:
            task = await run_service(
                tasks_service.claim,
                who.name or assignee_id,
                project=project,
                lease_s=lease_s,
                session_id=who.session_id,
                allowed_projects=who.projects,
            )
        except TaskError as exc:
            return _out(envelope(exc.code, exc.message))

        if task is not None:
            await events.notify_change()
            return _out(envelope(Outcome.claimed, task=task))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _out(envelope(Outcome.empty, task=None))
        # Capped for the same reason as the JSON route: see POLL_CEILING_S.
        await events.wait_for_task(min(remaining, POLL_CEILING_S))


async def heartbeat(
    task_id: int,
    assignee_id: str | int,
    lease_s: float | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    who = _who(ctx)
    # The answer carries the whole task, result included: a read before a write.
    existing = await run_service(tasks_service.get, task_id)
    if existing is not None and not who.may_touch(existing.project):
        return _out(envelope(Outcome.forbidden, f"project {existing.project} is outside the key's scope", task=None))
    try:
        outcome, task = await run_service(
            tasks_service.heartbeat,
            task_id=task_id,
            assignee_id=who.name or assignee_id,
            lease_s=lease_s,
            session_id=who.session_id,
            strict_session=who.agent is not None and not who.is_admin,
        )
    except TaskError as exc:
        # Without this the reason never reaches the model: the SDK reports an
        # unexpected tool error and drops the message.
        return _out(envelope(exc.code, exc.message))
    message = {
        Outcome.not_found: f"task {task_id} does not exist",
        Outcome.status_conflict: f"the task is not in progress but {task.status.value if task else ''}",
        Outcome.not_owner: f"the task is held by {task.assignee_id if task else ''}",
        Outcome.stale_session: "another instance of this agent holds the task",
    }.get(outcome)
    return _out(envelope(outcome, message, task=task))


#: The transport session manager may only be run once per instance, so the
#: server is built by a factory: every application gets its own.
def build() -> MCPServer:
    server = MCPServer(name="noted", instructions=INSTRUCTIONS)
    server.tool(description=SET_TASK)(set_task)
    server.tool(description=GET_TASKS)(get_tasks)
    server.tool(description=GET_TASK)(get_task)
    server.tool(description=SET_STATUS)(set_status)
    server.tool(description=CLAIM_TASK)(claim_task)
    server.tool(description=HEARTBEAT)(heartbeat)
    return server
