"""The JSON API: a case per outcome."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    with TestClient(main.create_app()) as c:
        yield c


def test_create_returns_201_created(client):
    response = client.post("/api/tasks", json={"task": {"title": "раз"}, "created_by": 42})
    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    assert payload["outcome"] == "created"
    assert payload["task"]["status"] == "pending"
    assert payload["task"]["created_by"] == "42", "an int id is normalised to a string"


def test_repeated_key_returns_exists(client):
    first = client.post("/api/tasks", json={"task": {"n": 1}, "key": "job-1"})
    second = client.post("/api/tasks", json={"task": {"n": 2}, "key": "job-1"})

    assert second.status_code == 200
    assert second.json()["outcome"] == "exists"
    assert second.json()["task"]["id"] == first.json()["task"]["id"]
    assert client.get("/api/tasks").json()["count"] == 1


def test_list_returns_ok_without_result_field(client):
    client.post("/api/tasks", json={"task": {"n": 1}})
    payload = client.get("/api/tasks", params={"status": "pending", "limit": 10}).json()

    assert payload["outcome"] == "ok"
    assert payload["count"] == 1
    assert "result" not in payload["tasks"][0]


def test_get_missing_task_returns_404_not_found(client):
    response = client.get("/api/tasks/999")
    assert response.status_code == 404
    assert response.json() == {
        "ok": False,
        "outcome": "not_found",
        "message": "задачи 999 не существует",
        "task": None,
    }


def test_status_conflict_returns_409_with_current_state(client):
    task_id = client.post("/api/tasks", json={"task": {"n": 1}}).json()["task"]["id"]
    client.post("/api/tasks/claim", json={"assignee_id": "agent-1"})
    client.patch(f"/api/tasks/{task_id}/status", json={"status": "done", "if_status": "in_progress"})

    response = client.patch(f"/api/tasks/{task_id}/status", json={"status": "failed", "if_status": "in_progress"})
    assert response.status_code == 409
    assert response.json()["outcome"] == "status_conflict"
    assert response.json()["task"]["status"] == "done"


def test_claim_then_empty(client):
    client.post("/api/tasks", json={"task": {"n": 1}})

    claimed = client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}).json()
    assert claimed["outcome"] == "claimed"
    assert claimed["task"]["assignee_id"] == "agent-1"
    assert claimed["task"]["status"] == "in_progress"

    empty = client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}).json()
    assert empty["outcome"] == "empty"
    assert empty["task"] is None
    assert empty["ok"] is True, "an empty queue is not an error"


def test_unknown_status_returns_422_validation_error(client):
    task_id = client.post("/api/tasks", json={"task": {"n": 1}}).json()["task"]["id"]
    response = client.patch(f"/api/tasks/{task_id}/status", json={"status": "почти_done"})

    assert response.status_code == 422
    assert response.json()["outcome"] == "validation_error"
    assert "status" in response.json()["message"]


def test_task_must_be_an_object(client):
    response = client.post("/api/tasks", json={"task": "строка"})
    assert response.status_code == 422
    assert response.json()["outcome"] == "validation_error"


def test_missing_parent_returns_404_parent_not_found(client):
    response = client.post("/api/tasks", json={"task": {"n": 1}, "parent_id": 404})
    assert response.status_code == 404
    assert response.json()["outcome"] == "parent_not_found"


def test_health_is_open(client):
    assert client.get("/healthz").json()["outcome"] == "ok"


def test_token_guards_api_but_not_health(client, monkeypatch):
    monkeypatch.setenv("NOTED_TOKEN", "s3cret")

    denied = client.get("/api/tasks")
    assert denied.status_code == 401
    assert denied.json()["outcome"] == "unauthorized"

    allowed = client.get("/api/tasks", headers={"X-Noted-Token": "s3cret"})
    assert allowed.status_code == 200
    assert client.get("/healthz").status_code == 200, "healthz needs no token"


def test_project_scope_through_the_api(client):
    client.post("/api/tasks", json={"task": {"n": 1}, "project": "noted"})
    client.post("/api/tasks", json={"task": {"n": 2}, "project": "abot"})

    scoped = client.get("/api/tasks", params={"project": "noted"}).json()
    assert scoped["count"] == 1
    assert scoped["tasks"][0]["project"] == "noted"

    wrong = client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "project": "other"}).json()
    assert wrong["outcome"] == "empty"

    right = client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "project": "abot"}).json()
    assert right["outcome"] == "claimed"
    assert right["task"]["task"] == {"n": 2}


async def test_long_poll_wakes_up_on_a_new_task():
    """This is why claim holds the connection: so agents never poll."""
    transport = httpx.ASGITransport(app=main.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        waiting = asyncio.create_task(
            client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "timeout_s": 10})
        )
        await asyncio.sleep(0.2)

        started = time.monotonic()
        await client.post("/api/tasks", json={"task": {"title": "разбуди меня"}})
        response = await asyncio.wait_for(waiting, timeout=5)
        elapsed = time.monotonic() - started

    assert response.json()["outcome"] == "claimed"
    assert elapsed < 1.0, f"проснулся за {elapsed:.2f}с — похоже, ждал по таймауту, а не по событию"


async def test_long_poll_gives_up_with_empty():
    transport = httpx.ASGITransport(app=main.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.monotonic()
        response = await client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "timeout_s": 0.3})
        elapsed = time.monotonic() - started

    assert response.json()["outcome"] == "empty"
    assert 0.25 < elapsed < 3.0


def test_heartbeat_extends_and_guards_ownership(client):
    task_id = client.post("/api/tasks", json={"task": {"n": 1}}).json()["task"]["id"]
    client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "lease_s": 30})

    extended = client.post(f"/api/tasks/{task_id}/heartbeat", json={"assignee_id": "agent-1", "lease_s": 60})
    assert extended.status_code == 200
    assert extended.json()["outcome"] == "updated"
    assert extended.json()["task"]["lease_expires"] is not None

    foreign = client.post(f"/api/tasks/{task_id}/heartbeat", json={"assignee_id": "agent-2"})
    assert foreign.status_code == 409
    assert foreign.json()["outcome"] == "not_owner"


def test_foreign_agent_cannot_close_the_task_through_the_api(client):
    task_id = client.post("/api/tasks", json={"task": {"n": 1}}).json()["task"]["id"]
    client.post("/api/tasks/claim", json={"assignee_id": "agent-1", "lease_s": 30})

    response = client.patch(
        f"/api/tasks/{task_id}/status", json={"status": "done", "assignee_id": "agent-2"}
    )
    assert response.status_code == 409
    assert response.json()["outcome"] == "not_owner"
    assert "agent-1" in response.json()["message"]


def test_events_endpoint_returns_the_journal(client):
    task_id = client.post("/api/tasks", json={"task": {"n": 1}, "created_by": "orchestrator"}).json()["task"]["id"]
    client.post("/api/tasks/claim", json={"assignee_id": "agent-1"})
    client.patch(f"/api/tasks/{task_id}/status", json={"status": "done"})

    payload = client.get(f"/api/tasks/{task_id}/events").json()
    assert payload["outcome"] == "ok"
    assert [e["event"] for e in payload["events"]] == ["created", "claimed", "status"]
    assert payload["count"] == 3

    assert client.get("/api/tasks/999/events").status_code == 404


def test_create_accepts_priority_attempts_and_dependencies(client):
    first = client.post("/api/tasks", json={"task": {"n": 1}}).json()["task"]
    second = client.post(
        "/api/tasks",
        json={"task": {"n": 2}, "priority": 5, "max_attempts": 3, "depends_on": [first["id"]]},
    ).json()["task"]

    assert second["priority"] == 5
    assert second["max_attempts"] == 3

    claimed = client.post("/api/tasks/claim", json={"assignee_id": "agent-1"}).json()
    assert claimed["task"]["id"] == first["id"], "a dependent task waits, priority or not"


def test_flood_is_answered_with_429(client, monkeypatch):
    monkeypatch.setenv("NOTED_CREATE_LIMIT", "2")

    for n in range(2):
        assert client.post("/api/tasks", json={"task": {"n": n}, "created_by": "agent-loop"}).status_code == 201

    response = client.post("/api/tasks", json={"task": {"n": 99}, "created_by": "agent-loop"})
    assert response.status_code == 429
    assert response.json()["outcome"] == "rate_limited"
    assert client.get("/api/tasks").json()["count"] == 2
