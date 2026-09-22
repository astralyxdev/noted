"""The event stream as a contract: typed entries and a lossless cursor."""

from __future__ import annotations

import json

import httpx

from api.services import tasks as service


def _read(lines, limit=12):
    """Reads SSE frames up to the first task event."""
    frame = {}
    for _ in range(limit):
        line = next(lines)
        if line.startswith("id: "):
            frame["id"] = int(line[4:])
        elif line.startswith("data: "):
            frame["data"] = json.loads(line[6:])
            return frame
    raise AssertionError("no event arrived")


def test_events_carry_the_transition_not_just_a_ping(live_server):
    with httpx.Client(base_url=live_server, timeout=15) as client:
        with client.stream("GET", "/events") as stream:
            lines = stream.iter_lines()
            assert "retry:" in next(lines)

            created = httpx.post(
                f"{live_server}/api/tasks",
                json={"task": {"title": "an event"}, "created_by": "orchestrator"},
                timeout=5,
            ).json()["task"]

            frame = _read(lines)

    assert frame["data"]["task_id"] == created["id"]
    assert frame["data"]["event"] == "created"
    assert frame["data"]["actor"] == "orchestrator"
    assert frame["data"]["to_status"] == "pending"
    assert frame["id"] == frame["data"]["id"], "the frame id is the cursor"


def test_reconnect_from_cursor_loses_nothing(live_server):
    """An integration reconnected: what it missed must arrive, not vanish."""
    task = service.create({"title": "while nobody listened"})[0]
    service.claim("agent-1")
    service.set_status(task.id, "done", actor="agent-1")

    with httpx.Client(base_url=live_server, timeout=15) as client:
        with client.stream("GET", "/events", params={"after": 0}) as stream:
            lines = stream.iter_lines()
            assert "retry:" in next(lines)
            seen = [_read(lines)["data"]["event"] for _ in range(3)]

    assert seen == ["created", "claimed", "status"], "history from zero is reachable by cursor"


def test_without_a_cursor_only_new_events_arrive(live_server):
    service.create({"title": "old"})

    with httpx.Client(base_url=live_server, timeout=15) as client:
        with client.stream("GET", "/events") as stream:
            lines = stream.iter_lines()
            assert "retry:" in next(lines)

            httpx.post(f"{live_server}/api/tasks", json={"task": {"title": "new"}}, timeout=5)
            frame = _read(lines)

    assert frame["data"]["event"] == "created"
    assert frame["data"]["task_id"] != 1, "without a cursor the past is not replayed"
