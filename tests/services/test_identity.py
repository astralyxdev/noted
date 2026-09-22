"""Agent identity and fencing by session."""

from __future__ import annotations

import time

import pytest

from api.models.envelope import Outcome
from api.services import agents
from api.services import tasks as service
from api.services.agents import AgentError


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

    outcome, _ = service.set_status(task.id, "done", actor="agent-1", session_id=second.id)
    assert outcome is Outcome.stale_session, "a second instance of the agent is not the owner"

    outcome, done = service.set_status(task.id, "done", actor="agent-1", session_id=first.id)
    assert outcome is Outcome.updated
    assert done.status.value == "done"


def test_dead_session_frees_everything_it_held_at_once(monkeypatch):
    monkeypatch.setenv("NOTED_SESSION_TTL_S", "0.05")
    for n in range(3):
        service.create({"n": n})

    session = agents.open_session("agent-1", "http")
    held = [service.claim("agent-1", session_id=session.id).id for _ in range(3)]
    assert all(service.get(i).status.value == "in_progress" for i in held)

    time.sleep(0.1)
    agents.expire_sessions()
    assert sorted(service.reap_expired()) == sorted(held), "the session died, so all its tasks came back"

    for task_id in held:
        back = service.get(task_id)
        assert back.status.value == "pending"
        assert back.session_id is None


def test_claim_inside_a_session_needs_no_lease():
    """A model cannot send heartbeats while a ten-minute build runs. Inside a
    session no task lease is set at all — the session holds it."""
    service.create({"title": "long build"})
    session = agents.open_session("agent-1", "stdio")

    taken = service.claim("agent-1", session_id=session.id)
    assert taken.lease_expires is None
    assert taken.session_id == session.id

    assert service.reap_expired() == [], "while the session lives the task is not taken"
