"""MCP-адаптер: пять инструментов из SPEC.md поверх HTTP-ядра.

Логики здесь нет намеренно. Каждый инструмент собирает тело, дёргает клиент и
отдаёт ответ как есть — форма ответа задаётся ядром в одном месте.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from api.utils.tool_docs import (
    CLAIM_TASK,
    GET_TASK,
    GET_TASKS,
    HARD_ERRORS,
    HEARTBEAT,
    SET_STATUS,
    SET_TASK,
)
from mcp_adapter.client import request

server = MCPServer(
    name="noted",
    instructions=(
        "Таск-менеджер для агентов. Ставьте задачи через set_task, забирайте работу через "
        "claim_task (timeout_s>0 — ждать появления задачи), отчитывайтесь через set_status. "
        "Каждый ответ содержит поле outcome — по нему судите об исходе операции."
    ),
)


def _check(payload: dict[str, Any]) -> dict[str, Any]:
    outcome = payload.get("outcome")
    if outcome in HARD_ERRORS:
        raise ToolError(f"{outcome}: {payload.get('message') or 'ошибка вызова'}")
    return payload


@server.tool(description=SET_TASK)
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
    payload = {
        "task": task,
        "project": project,
        "assignee_id": assignee_id,
        "parent_id": parent_id,
        "key": key,
        "created_by": created_by,
        "priority": priority,
        "max_attempts": max_attempts,
        "depends_on": depends_on,
    }
    return _check(await request("POST", "/api/tasks", json=payload))


@server.tool(description=GET_TASKS)
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
    params: list[tuple[str, Any]] = []
    if assignee_id is not None:
        params.append(("assignee_id", assignee_id))
    if project is not None:
        params.append(("project", project))
    if unscoped:
        params.append(("unscoped", "true"))
    for value in [status] if isinstance(status, str) else (status or []):
        params.append(("status", value))
    if unassigned:
        params.append(("unassigned", "true"))
    if parent_id is not None:
        params.append(("parent_id", parent_id))
    if stale_seconds is not None:
        params.append(("stale_seconds", stale_seconds))
    params.append(("limit", limit))
    return _check(await request("GET", "/api/tasks", params=params))


@server.tool(description=GET_TASK)
async def get_task(task_id: int, with_events: bool = False) -> dict[str, Any]:
    path = f"/api/tasks/{task_id}/events" if with_events else f"/api/tasks/{task_id}"
    return _check(await request("GET", path))


@server.tool(description=SET_STATUS)
async def set_status(
    task_id: int,
    status: str,
    result: Any = None,
    if_status: str | None = None,
    assignee_id: str | int | None = None,
) -> dict[str, Any]:
    payload = {"status": status, "result": result, "if_status": if_status, "assignee_id": assignee_id}
    return _check(await request("PATCH", f"/api/tasks/{task_id}/status", json=payload))


@server.tool(description=CLAIM_TASK)
async def claim_task(
    assignee_id: str | int,
    project: str | None = None,
    timeout_s: float = 0,
    lease_s: float | None = None,
) -> dict[str, Any]:
    payload = {
        "assignee_id": assignee_id,
        "project": project,
        "timeout_s": timeout_s,
        "lease_s": lease_s,
    }
    return _check(await request("POST", "/api/tasks/claim", json=payload, timeout=timeout_s + 10.0))


@server.tool(description=HEARTBEAT)
async def heartbeat(task_id: int, assignee_id: str | int, lease_s: float | None = None) -> dict[str, Any]:
    payload = {"assignee_id": assignee_id, "lease_s": lease_s}
    return _check(await request("POST", f"/api/tasks/{task_id}/heartbeat", json=payload))


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
