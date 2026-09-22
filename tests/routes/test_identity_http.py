"""Identity over a real transport: the key, the scope and the session."""

from __future__ import annotations

import httpx
import pytest
from api.services import agents
from api.services import tasks as service


@pytest.fixture
def key():
    return agents.issue("agent-1", projects=["noted"]).key


def test_key_decides_who_you_are(live_server, key):
    """assignee_id and created_by come from the key, not from the request body."""
    with httpx.Client(base_url=live_server, headers={"X-Noted-Token": key}, timeout=10) as client:
        created = client.post(
            "/api/tasks",
            json={"task": {"n": 1}, "project": "noted", "created_by": "кто-то другой"},
        ).json()
        assert created["task"]["created_by"] == "agent-1", "authorship cannot be forged"

        claimed = client.post("/api/tasks/claim", json={"assignee_id": "чужое-имя"}).json()
        assert claimed["task"]["assignee_id"] == "agent-1", "you cannot take another name"


def test_scope_is_enforced_over_http(live_server, key):
    service.create({"n": 1}, project="abot")

    with httpx.Client(base_url=live_server, headers={"X-Noted-Token": key}, timeout=10) as client:
        denied = client.post("/api/tasks", json={"task": {"n": 2}, "project": "abot"})
        assert denied.status_code == 403
        assert denied.json()["outcome"] == "forbidden"

        empty = client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}).json()
        assert empty["outcome"] == "empty", "a foreign project is invisible, not just empty"


def test_unknown_key_is_refused(live_server, key):
    with httpx.Client(base_url=live_server, headers={"X-Noted-Token": "noted_forged"}, timeout=10) as client:
        assert client.get("/api/tasks").status_code == 401

    with httpx.Client(base_url=live_server, timeout=10) as client:
        assert client.get("/api/tasks").status_code == 401, "no key, no entry"


def test_revoked_key_stops_working(live_server, key):
    with httpx.Client(base_url=live_server, headers={"X-Noted-Token": key}, timeout=10) as client:
        assert client.get("/api/tasks").status_code == 200
        agents.revoke("agent-1")
        assert client.get("/api/tasks").status_code == 401


def test_transport_session_holds_the_task_without_any_lease(live_server, key):
    """The transport session is the agent session: no time-based lease needed.

    An MCP client sends Mcp-Session-Id by itself; here the same header is set
    by hand, because what is under test is the server, not the SDK client.
    """
    service.create({"title": "долгая сборка"}, project="noted")

    headers = {"X-Noted-Token": key, "Mcp-Session-Id": "session-of-a-live-process"}
    with httpx.Client(base_url=live_server, headers=headers, timeout=10) as client:
        claimed = client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}).json()

    assert claimed["outcome"] == "claimed"
    assert claimed["task"]["lease_expires"] is None, "held by the session, not a timer"
    assert claimed["task"]["session_id"] == "session-of-a-live-process"

    # While the session lives the collector leaves the task alone.
    assert service.reap_expired() == []


def test_a_second_instance_of_the_same_agent_cannot_write(live_server, key):
    """Fencing over the transport: same name, different instance."""
    task = service.create({"title": "спорная"}, project="noted")[0]

    first = {"X-Noted-Token": key, "X-Noted-Session": "instance-one"}
    second = {"X-Noted-Token": key, "X-Noted-Session": "instance-two"}

    with httpx.Client(base_url=live_server, timeout=10) as client:
        client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}, headers=first)

        zombie = client.patch(f"/api/tasks/{task.id}/status", json={"status": "done"}, headers=second)
        assert zombie.status_code == 409
        assert zombie.json()["outcome"] == "stale_session"

        owner = client.patch(f"/api/tasks/{task.id}/status", json={"status": "done"}, headers=first)
        assert owner.json()["outcome"] == "updated"


def test_dashboard_has_its_own_door(live_server, key, monkeypatch):
    """A browser sends no headers, so the dashboard gets a door and a cookie."""
    monkeypatch.setenv("NOTED_TOKEN", "admin-secret")

    with httpx.Client(base_url=live_server, timeout=10) as browser:
        assert browser.get("/api/tasks").status_code == 401

        assert browser.post("/api/login", json={"token": "wrong"}).json()["outcome"] == "unauthorized"
        assert browser.post("/api/login", json={"token": key}).json()["outcome"] == "unauthorized", (
            "an agent key is not a way into the dashboard; that takes the admin token"
        )

        assert browser.post("/api/login", json={"token": "admin-secret"}).json()["outcome"] == "ok"
        assert browser.get("/api/tasks").status_code == 200, "the cookie works after login"
