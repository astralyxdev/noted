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
    assert names == {"set_task", "get_tasks", "get_task", "set_status", "claim_task", "heartbeat"}


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


async def test_lease_and_heartbeat_over_http(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        created = (await mcp.call_tool("set_task", {"task": {"title": "долгая"}})).structured_content
        task_id = created["task"]["id"]

        claimed = (
            await mcp.call_tool("claim_task", {"assignee_id": "agent-1", "lease_s": 30})
        ).structured_content
        assert claimed["outcome"] == "claimed"
        assert claimed["task"]["lease_expires"] is not None

        beat = (
            await mcp.call_tool("heartbeat", {"task_id": task_id, "assignee_id": "agent-1", "lease_s": 60})
        ).structured_content
        assert beat["outcome"] == "updated"

        foreign = (
            await mcp.call_tool("heartbeat", {"task_id": task_id, "assignee_id": "agent-2"})
        ).structured_content
        assert foreign["outcome"] == "not_owner"


async def test_journal_is_available_to_the_agent(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        created = (await mcp.call_tool("set_task", {"task": {"title": "с журналом"}})).structured_content
        task_id = created["task"]["id"]
        await mcp.call_tool("claim_task", {"assignee_id": "agent-1"})

        with_log = (
            await mcp.call_tool("get_task", {"task_id": task_id, "with_events": True})
        ).structured_content
        assert [e["event"] for e in with_log["events"]] == ["created", "claimed"]

        without = (await mcp.call_tool("get_task", {"task_id": task_id})).structured_content
        assert "events" not in without


async def test_flood_reaches_the_agent_as_an_error(live_server, monkeypatch):
    """Захлебнувшийся агент должен получить исключение, а не тихое «нет»:
    иначе цикл, который его туда загнал, продолжит крутиться."""
    monkeypatch.setenv("NOTED_CREATE_LIMIT", "2")

    async with Client(f"{live_server}/mcp/") as mcp:
        for n in range(2):
            await mcp.call_tool("set_task", {"task": {"n": n}, "created_by": "agent-loop"})

        blocked = await mcp.call_tool("set_task", {"task": {"n": 99}, "created_by": "agent-loop"})
        assert blocked.is_error
        assert "rate_limited" in str(blocked.content[0].text)

        other = await mcp.call_tool("set_task", {"task": {"n": 1}, "created_by": "agent-other"})
        assert other.structured_content["outcome"] == "created"
