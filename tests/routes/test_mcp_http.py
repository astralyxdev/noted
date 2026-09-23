"""MCP over HTTP, on the same port as the API.

The point of the endpoint is that an agent needs no separate process: the
container is up, so MCP is already there. Checked with a real SDK client.

The client is opened inside each test rather than by a fixture: anyio will not
let a cancel scope be exited from another task, which is what a yielding
fixture does.
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
            await mcp.call_tool("set_task", {"task": {"title": "over http"}, "project": "noted"})
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
        assert empty["outcome"] == "empty", "an empty queue is not an error"

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
        result = await mcp.call_tool("set_status", {"task_id": 1, "status": "almost_done"})
    assert result.is_error, "bad input must arrive as a tool error"


async def test_missing_task_is_a_normal_answer(live_server):
    """not_found is a normal outcome: an agent must handle it, not crash."""
    async with Client(f"{live_server}/mcp/") as mcp:
        result = await mcp.call_tool("get_task", {"task_id": 999})
    assert not result.is_error
    assert result.structured_content["outcome"] == "not_found"


async def test_lease_and_heartbeat_over_http(live_server):
    async with Client(f"{live_server}/mcp/") as mcp:
        created = (await mcp.call_tool("set_task", {"task": {"title": "long"}})).structured_content
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
        created = (await mcp.call_tool("set_task", {"task": {"title": "with a journal"}})).structured_content
        task_id = created["task"]["id"]
        await mcp.call_tool("claim_task", {"assignee_id": "agent-1"})

        with_log = (
            await mcp.call_tool("get_task", {"task_id": task_id, "with_events": True})
        ).structured_content
        assert [e["event"] for e in with_log["events"]] == ["created", "claimed"]

        without = (await mcp.call_tool("get_task", {"task_id": task_id})).structured_content
        assert "events" not in without


async def test_flood_reaches_the_agent_as_an_error(live_server, monkeypatch):
    """A flooding agent needs an exception, not a quiet "no": otherwise the
    loop that got it there keeps spinning."""
    monkeypatch.setenv("NOTED_CREATE_LIMIT", "2")

    async with Client(f"{live_server}/mcp/") as mcp:
        for n in range(2):
            await mcp.call_tool("set_task", {"task": {"n": n}, "created_by": "agent-loop"})

        blocked = await mcp.call_tool("set_task", {"task": {"n": 99}, "created_by": "agent-loop"})
        assert blocked.is_error
        assert "rate_limited" in str(blocked.content[0].text)

        other = await mcp.call_tool("set_task", {"task": {"n": 1}, "created_by": "agent-other"})
        assert other.structured_content["outcome"] == "created"


def _documented() -> dict[str, dict[str, tuple[bool, str]]]:
    """MCP.md's tables, read back as {tool: {parameter: (required, default)}}."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "MCP.md").read_text(encoding="utf-8")
    tools: dict[str, dict[str, tuple[bool, str]]] = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"### `(\w+)`", line)
        if heading:
            current = heading.group(1)
            tools[current] = {}
            continue
        row = re.match(r"\| `(\w+)` \| [^|]+ \| (\*\*yes\*\*|no) \| (.+?) \|", line)
        if row and current:
            tools[current][row.group(1)] = (row.group(2) == "**yes**", row.group(3).strip())
    return tools


async def test_mcp_md_describes_the_tools_the_server_actually_publishes(live_server):
    """A hand-written reference goes stale the first time a parameter changes.

    So it is checked against the schema the server publishes: every tool, every
    parameter, and whether it is required. Defaults are compared too, because
    `lease_s: null` meaning 300 seconds rather than "no lease" is exactly the
    kind of detail somebody plans their heartbeats around.
    """
    documented = _documented()
    assert documented, "no tables were parsed from MCP.md — the format changed"

    async with Client(f"{live_server}/mcp/") as mcp:
        published = {t.name: t.input_schema for t in (await mcp.list_tools()).tools}

    assert set(documented) == set(published), (
        f"MCP.md and the server disagree on which tools exist: "
        f"documented only {sorted(set(documented) - set(published))}, "
        f"published only {sorted(set(published) - set(documented))}"
    )

    def rendered(spec: dict, name: str, required: set[str]) -> str:
        if name in required:
            return "—"
        default = spec.get("default", None)
        return {None: "`null`", True: "`true`", False: "`false`"}.get(
            default if isinstance(default, bool) or default is None else object(), f"`{default}`"
        )

    for name, schema in published.items():
        required = set(schema.get("required", ()))
        for parameter, spec in schema.get("properties", {}).items():
            assert parameter in documented[name], f"MCP.md does not document {name}.{parameter}"
            says_required, says_default = documented[name][parameter]
            assert says_required == (parameter in required), (
                f"MCP.md has {name}.{parameter} as "
                f"{'required' if says_required else 'optional'}, the server disagrees"
            )
            assert says_default == rendered(spec, parameter, required), (
                f"MCP.md gives {name}.{parameter} a default of {says_default}, "
                f"the server says {rendered(spec, parameter, required)}"
            )
        extra = set(documented[name]) - set(schema.get("properties", {}))
        assert not extra, f"MCP.md documents parameters {name} does not have: {sorted(extra)}"
