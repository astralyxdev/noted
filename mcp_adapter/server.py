"""MCP-адаптер: пять инструментов из SPEC.md поверх HTTP-ядра.

Логики здесь нет намеренно. Каждый инструмент собирает тело, дёргает клиент и
отдаёт ответ как есть — форма ответа задаётся ядром в одном месте.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_adapter.client import request

#: Исходы, которые агент должен увидеть как ошибку инструмента, а не как результат.
#: not_found, status_conflict и empty сюда не входят: это штатные ответы.
HARD_ERRORS = {"unauthorized", "validation_error", "api_unavailable", "internal_error"}

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


@server.tool()
async def set_task(
    task: dict[str, Any],
    project: str | None = None,
    assignee_id: str | int | None = None,
    parent_id: int | None = None,
    key: str | None = None,
    created_by: str | int | None = None,
) -> dict[str, Any]:
    """Поставить задачу.

    task — произвольный JSON-объект с описанием работы. project задаёт скоуп:
    агент, работающий в этом проекте, заберёт только его задачи. assignee_id=null
    кладёт задачу в общий пул, откуда её возьмёт первый свободный агент. parent_id
    связывает подзадачу с родительской. key — ключ идемпотентности: повторный
    вызов с тем же ключом не создаст дубль и вернёт outcome="exists".
    Исходы: created, exists.
    """
    payload = {
        "task": task,
        "project": project,
        "assignee_id": assignee_id,
        "parent_id": parent_id,
        "key": key,
        "created_by": created_by,
    }
    return _check(await request("POST", "/api/tasks", json=payload))


@server.tool()
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
    """Список задач, свежие первыми.

    status — строка или список (["pending","in_progress"] = всё открытое).
    project сужает до одного проекта, unscoped=true — только задачи без проекта.
    unassigned=true — только общий пул. stale_seconds — задачи, не обновлявшиеся
    дольше N секунд: так находятся зависшие in_progress после падения агента.
    Поле result в списке не приходит — за ним идите в get_task. Исход: ok.
    """
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


@server.tool()
async def get_task(task_id: int) -> dict[str, Any]:
    """Одна задача целиком, вместе с result. Исходы: ok, not_found."""
    return _check(await request("GET", f"/api/tasks/{task_id}"))


@server.tool()
async def set_status(
    task_id: int,
    status: str,
    result: Any = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """Сменить статус задачи и приложить результат.

    status: pending | in_progress | blocked | done | failed | cancelled.
    result пишется и для успеха, и для ошибки (при failed кладите туда причину).
    if_status делает вызов compare-and-set: запись пройдёт, только если задача
    всё ещё в этом статусе, иначе придёт outcome="status_conflict" с актуальным
    состоянием — так двое не доделывают одну задачу.
    Исходы: updated, not_found, status_conflict.
    """
    payload = {"status": status, "result": result, "if_status": if_status}
    return _check(await request("PATCH", f"/api/tasks/{task_id}/status", json=payload))


@server.tool()
async def claim_task(assignee_id: str | int, project: str | None = None, timeout_s: float = 0) -> dict[str, Any]:
    """Атомарно забрать следующую задачу и перевести её в in_progress.

    Сначала выдаются задачи, адресованные этому assignee_id, затем общий пул,
    внутри группы — FIFO. Двое не могут забрать одну задачу.
    project сужает выборку строго: назвав проект, агент не заберёт ни чужую
    задачу, ни задачу без проекта.
    timeout_s=0 — забрать, если есть; timeout_s>0 — ждать появления до N секунд
    (это дешевле, чем крутить опрос). Если ждать нечего, придёт outcome="empty".
    Исходы: claimed, empty.
    """
    payload = {"assignee_id": assignee_id, "project": project, "timeout_s": timeout_s}
    return _check(await request("POST", "/api/tasks/claim", json=payload, timeout=timeout_s + 10.0))


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
