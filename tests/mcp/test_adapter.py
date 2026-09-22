"""The stdio adapter: its tools reach a live core over HTTP."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_adapter.server import claim_task, get_task, get_tasks, set_status, set_task
from tests.conftest import free_port


async def test_full_cycle_through_tools(live_server):
    created = await set_task({"title": "прогнать тесты"}, created_by="orchestrator", key="job-1")
    assert created["outcome"] == "created"
    task_id = created["task"]["id"]

    again = await set_task({"title": "прогнать тесты"}, key="job-1")
    assert again["outcome"] == "exists"
    assert again["task"]["id"] == task_id

    claimed = await claim_task("agent-1")
    assert claimed["outcome"] == "claimed"
    assert claimed["task"]["id"] == task_id

    assert (await claim_task("agent-1"))["outcome"] == "empty"

    finished = await set_status(task_id, "done", result={"passed": 13}, if_status="in_progress")
    assert finished["outcome"] == "updated"

    conflict = await set_status(task_id, "failed", if_status="in_progress")
    assert conflict["outcome"] == "status_conflict"
    assert conflict["task"]["status"] == "done"

    assert (await get_task(task_id))["task"]["result"] == {"passed": 13}
    assert (await get_task(999))["outcome"] == "not_found"

    listed = await get_tasks(status=["done", "pending"])
    assert listed["count"] == 1
    assert "result" not in listed["tasks"][0]


async def test_validation_error_is_raised_as_tool_error(live_server):
    with pytest.raises(ToolError) as err:
        await set_status(1, "почти_done")
    assert "validation_error" in str(err.value)


async def test_dead_core_reports_api_unavailable(monkeypatch):
    monkeypatch.setenv("NOTED_API", f"http://127.0.0.1:{free_port()}")

    with pytest.raises(ToolError) as err:
        await get_tasks()

    message = str(err.value)
    assert "api_unavailable" in message
    assert "noted-api" in message, "the message must say what to start"
