"""Agent identity and fencing by session."""

from __future__ import annotations

import time

import pytest

from api.models.envelope import Outcome
from api.services import agents
from api.services import tasks as service
from api.services.agents import AgentError


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    """Identity may not be switched on without a way into the dashboard."""
    monkeypatch.setenv("NOTED_TOKEN", "admin-secret")


def test_key_proves_identity_and_revocation_takes_it_away():
    issued = agents.issue("agent-1", projects=["noted"])

    assert agents.resolve(issued.key).id == "agent-1"
    assert agents.resolve("noted_forged") is None, "an unknown key does not pass"

    agents.revoke("agent-1")
    assert agents.resolve(issued.key) is None


def test_identity_is_off_until_the_first_key_is_issued():
    """An upgrade breaks no local install: strictness is switched on explicitly."""
    assert agents.identity_required() is False
    agents.issue("agent-1")
    assert agents.identity_required() is True


def test_names_are_unique():
    agents.issue("agent-1")
    with pytest.raises(AgentError):
        agents.issue("agent-1")


def test_scope_is_enforced_not_advisory():
    scoped = agents.issue("agent-1", projects=["noted"]).agent
    assert scoped.may_touch("noted") is True
    assert scoped.may_touch("abot") is False
    assert scoped.may_touch(None) is True, "the shared pool is open to everyone"

    assert agents.issue("agent-any").agent.may_touch("abot") is True


def test_scoped_claim_never_reaches_a_foreign_project():
    service.create({"n": 1}, project="abot")
    service.create({"n": 2}, project="noted")
    service.create({"n": 3})

    taken = service.claim("agent-1", allowed_projects=["noted"])
    assert taken.project == "noted"

    taken = service.claim("agent-1", allowed_projects=["noted"])
    assert taken.project is None, "the shared pool stays reachable"

    assert service.claim("agent-1", allowed_projects=["noted"]) is None, "a foreign project is invisible"

    with pytest.raises(service.TaskError) as err:
        service.claim("agent-1", project="abot", allowed_projects=["noted"])
    assert err.value.code is Outcome.forbidden


def test_zombie_with_an_old_session_cannot_write():
    """The same key can run twice. A task is held by an instance, not a name."""
    task, _ = service.create({"title": "contested"})
    first = agents.open_session("agent-1", "stdio")
    second = agents.open_session("agent-1", "stdio")

    service.claim("agent-1", session_id=first.id)

    outcome, _ = service.set_status(task.id, "done", actor="agent-1", session_id=second.id, strict_session=True)
    assert outcome is Outcome.stale_session, "a second instance of the agent is not the owner"

    outcome, _ = service.set_status(task.id, "done", actor="agent-1", strict_session=True)
    assert outcome is Outcome.stale_session, "dropping the header is not a way around fencing"

    outcome, done = service.set_status(task.id, "done", actor="agent-1", session_id=first.id, strict_session=True)
    assert outcome is Outcome.updated
    assert done.status.value == "done"


def test_dead_session_frees_everything_it_held_at_once(monkeypatch):
    monkeypatch.setenv("NOTED_SESSION_TTL_S", "0.05")
    for n in range(3):
        service.create({"n": n})

    session = agents.open_session("agent-1", "stdio")
    held = [service.claim("agent-1", session_id=session.id).id for _ in range(3)]
    assert all(service.get(i).status.value == "in_progress" for i in held)

    time.sleep(0.1)
    assert sorted(service.reap_expired()) == sorted(held), "the session went quiet, so its tasks came back"

    for task_id in held:
        assert service.get(task_id).status.value == "pending"

    # The freed task carries no session, so the next holder is not fenced out.
    fresh = agents.open_session("agent-2", "stdio")
    taken = service.claim("agent-2", session_id=fresh.id)
    assert taken.id in held
    outcome, _ = service.set_status(taken.id, "done", actor="agent-2", session_id=fresh.id, strict_session=True)
    assert outcome is Outcome.updated


def test_a_live_session_holds_the_task_past_its_lease():
    """A model cannot send heartbeats while a ten-minute build runs, so a live
    session holds the task even after the lease is gone.

    The lease is still taken: only some transports prove they are alive, and a
    session alone would hand an HTTP client's task away after ninety seconds of
    silence."""
    service.create({"title": "long build"})
    session = agents.open_session("agent-1", "stdio")

    taken = service.claim("agent-1", session_id=session.id, lease_s=0.01)
    assert taken.lease_expires is not None, "a lease is taken inside a session too"

    time.sleep(0.05)
    assert service.reap_expired() == [], "the lease is gone, but the session still holds it"
    assert service.get(taken.id).status.value == "in_progress"


def test_a_silent_keepalive_session_releases_the_task_at_once(monkeypatch):
    """The stdio adapter renews in the background, so silence there means a dead
    process rather than a busy one — no reason to wait out the lease."""
    monkeypatch.setenv("NOTED_SESSION_TTL_S", "0.05")
    task, _ = service.create({"title": "abandoned"})
    session = agents.open_session("agent-1", "stdio")
    service.claim("agent-1", session_id=session.id, lease_s=600)

    time.sleep(0.1)
    assert service.reap_expired() == [task.id]
    assert service.get(task.id).status.value == "pending"


def test_an_http_session_falls_back_to_the_lease(monkeypatch):
    """An HTTP client sends nothing during a long step, so its silence proves
    nothing and the lease is what governs recovery."""
    monkeypatch.setenv("NOTED_SESSION_TTL_S", "0.05")
    task, _ = service.create({"title": "long build over http"})
    session = agents.open_session("agent-1", "http-mcp")
    service.claim("agent-1", session_id=session.id, lease_s=600)

    time.sleep(0.1)
    assert service.reap_expired() == [], "a quiet HTTP client is not proof of death"
    assert service.get(task.id).status.value == "in_progress"
