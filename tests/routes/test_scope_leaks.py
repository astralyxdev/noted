"""Everything a scoped key must not be able to read.

The scope was enforced on the obvious routes and missed on the ones that also
return data: a heartbeat answers with the whole task, `/events` replays every
project's journal, and `/api/stats` counts and names them. Each of these was
reachable with a key that is refused on `GET /api/tasks/<id>`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from api.services import agents


@pytest.fixture
def client():
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture
def keys():
    """An admin, and an agent that may only see project `mine`."""
    admin = agents.issue("root", is_admin=True).key
    scoped = agents.issue("narrow", projects=["mine"]).key
    return {"admin": {"X-Noted-Token": admin}, "scoped": {"X-Noted-Token": scoped}}


def _secret_task(client, keys) -> int:
    """A finished task in a project the scoped key knows nothing about."""
    made = client.post(
        "/api/tasks",
        json={"task": {"what": "secret"}, "project": "theirs"},
        headers=keys["admin"],
    ).json()["task"]
    client.post("/api/tasks/claim", json={"assignee_id": "root", "project": "theirs"}, headers=keys["admin"])
    client.patch(
        f"/api/tasks/{made['id']}/status",
        json={"status": "done", "result": {"api_key": "TOP-SECRET"}},
        headers=keys["admin"],
    )
    return made["id"]


def test_heartbeat_does_not_hand_over_a_foreign_task(client, keys):
    """The reply carries the task, result and all — a read before it is a write."""
    task_id = _secret_task(client, keys)

    assert client.get(f"/api/tasks/{task_id}", headers=keys["scoped"]).json()["outcome"] == "forbidden"

    answer = client.post(
        f"/api/tasks/{task_id}/heartbeat", json={"assignee_id": "narrow"}, headers=keys["scoped"]
    ).json()
    assert answer["outcome"] == "forbidden", "heartbeat leaked a task from another project"
    assert answer["task"] is None


def test_the_journal_itself_is_scoped(client, keys):
    """`?after=0` replays history, so the replay is where a scope has to hold.

    Checked on the service the stream reads from: the stream never ends, and a
    test that waits for it to finish waits for ever.
    """
    from api.services import tasks as service

    _secret_task(client, keys)
    client.post("/api/tasks", json={"task": {"ok": True}, "project": "mine"}, headers=keys["admin"])

    everything = service.events_after(0, 200)
    scoped = service.events_after(0, 200, allowed_projects=["mine"])

    assert len(everything) > len(scoped), "the scope filtered nothing"
    seen = {service.get(e.task_id).project for e in scoped}
    assert seen <= {"mine", None}, f"the journal of another project reached a scoped key: {seen}"


def test_the_event_route_asks_for_the_caller_scope(live_server):
    """And the route must actually pass the principal down to it."""
    import httpx

    from api.services import agents

    admin = agents.issue("root2", is_admin=True).key
    scoped = agents.issue("narrow2", projects=["mine"]).key

    made = httpx.post(f"{live_server}/api/tasks", headers={"X-Noted-Token": admin},
                      json={"task": {"what": "secret"}, "project": "theirs"}, timeout=5).json()["task"]
    httpx.post(f"{live_server}/api/tasks", headers={"X-Noted-Token": admin},
               json={"task": {"ok": True}, "project": "mine"}, timeout=5)

    with httpx.Client(base_url=live_server, timeout=15) as client:
        with client.stream("GET", "/events?after=0", headers={"X-Noted-Token": scoped}) as stream:
            body = ""
            for line in stream.iter_lines():
                body += line
                # Both tasks exist; once the visible one has arrived, whatever
                # was going to be replayed has been.
                if f'"task_id": {made["id"] + 1}' in body:
                    break

    assert f'"task_id": {made["id"]}' not in body, "a foreign project's entry was replayed"
    assert "theirs" not in body


def test_stats_counts_and_names_only_what_the_key_may_see(client, keys):
    """Counters, project names and assignee names are all readable data."""
    _secret_task(client, keys)
    client.post("/api/tasks", json={"task": {"ok": True}, "project": "mine"}, headers=keys["admin"])

    refused = client.get("/api/stats?project=theirs", headers=keys["scoped"]).json()
    assert refused["outcome"] == "forbidden", "counters for another project were handed over"

    mine = client.get("/api/stats", headers=keys["scoped"]).json()
    assert mine["projects"] == ["mine"], f"project names leaked: {mine['projects']}"
    assert "root" not in mine["assignees"], f"assignee names leaked: {mine['assignees']}"


def test_a_foreign_task_cannot_be_used_as_a_dependency_or_parent(client, keys):
    """Pointing at a task is a way of reading it: `waiting_on` then reports
    whether it has finished. Out of scope answers "does not exist", so the ids
    cannot be enumerated either."""
    task_id = _secret_task(client, keys)

    as_dep = client.post(
        "/api/tasks",
        json={"task": {"n": 1}, "project": "mine", "depends_on": [task_id]},
        headers=keys["scoped"],
    ).json()
    assert as_dep["outcome"] == "validation_error"
    assert "does not exist" in as_dep["message"]
    assert "theirs" not in as_dep["message"], "the refusal named the other project"

    as_parent = client.post(
        "/api/tasks",
        json={"task": {"n": 2}, "project": "mine", "parent_id": task_id},
        headers=keys["scoped"],
    ).json()
    assert as_parent["outcome"] == "parent_not_found"
