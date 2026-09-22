"""MCP по HTTP на том же порту, что и API.

Смысл эндпоинта в том, что агенту не нужен отдельный процесс: поднялся
контейнер — MCP уже доступен. Проверяем настоящим клиентом из SDK.

Клиент открывается внутри теста, а не фикстурой: anyio не переносит выход из
cancel scope в другую задачу, а фикстура с yield делает ровно это.
"""

from __future__ import annotations

from mcp.client.client import Client


async def test_tools_are_published(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        names = {tool.name for tool in (await mcp.list_tools()).tools}
    assert names == {"set_task", "get_tasks", "get_task", "set_status", "claim_task"}


async def test_full_cycle_over_http(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        created = (
            await mcp.call_tool("set_task", {"task": {"title": "через http"}, "project": "noted"})
        ).structured_content
        assert created["outcome"] == "created"
        task_id = created["task"]["id"]

        claimed = (
            await mcp.call_tool("claim_task", {"assignee_id": "agent-1", "project": "noted"})
        ).structured_content
        assert claimed["outcome"] == "claimed"
        assert claimed["task"]["id"] == task_id

        empty = (
            await mcp.call_tool("claim_task", {"assignee_id": "agent-1", "project": "noted"})
        ).structured_content
        assert empty["outcome"] == "empty", "чужой скоуп и пустая очередь — не ошибка"

        done = (
            await mcp.call_tool(
                "set_status",
                {"task_id": task_id, "status": "done", "result": {"ok": True}, "if_status": "in_progress"},
            )
        ).structured_content
        assert done["outcome"] == "updated"

        conflict = (
            await mcp.call_tool("set_status", {"task_id": task_id, "status": "failed", "if_status": "in_progress"})
        ).structured_content
        assert conflict["outcome"] == "status_conflict"

        fetched = (await mcp.call_tool("get_task", {"task_id": task_id})).structured_content
        assert fetched["task"]["result"] == {"ok": True}

        listed = (await mcp.call_tool("get_tasks", {"project": "noted"})).structured_content
        assert listed["count"] == 1


async def test_bad_input_is_reported_as_tool_error(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        result = await mcp.call_tool("set_status", {"task_id": 1, "status": "почти_done"})
    assert result.is_error, "кривой вход должен приходить как ошибка инструмента"


async def test_missing_task_is_a_normal_answer(live_server):
    """not_found — штатный исход: агент обязан его обработать, а не упасть."""
    async with Client(f"{live_server}/mcp/") as mcp:
        result = await mcp.call_tool("get_task", {"task_id": 999})
    assert not result.is_error
    assert result.structured_content["outcome"] == "not_found"
