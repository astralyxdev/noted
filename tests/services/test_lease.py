"""Leases, heartbeats and returning a task to the queue automatically.

This is the difference between "you can see it is stuck" and "it fixed
itself": without a lease a crashed agent leaves its task in_progress forever.
"""

from __future__ import annotations

import time

from api.models.envelope import Outcome
from api.services import tasks as service


def test_expired_lease_returns_the_task_to_the_queue():
    task, _ = service.create({"title": "long"})
    taken = service.claim("agent-1", lease_s=0.01)
    assert taken.id == task.id
    assert taken.lease_expires is not None

    time.sleep(0.05)
    assert service.reap_expired() == [task.id]

    back = service.get(task.id)
    assert back.status.value == "pending"
    assert back.assignee_id is None, "back to the shared pool, not held by a dead agent"
    assert back.attempts == 1, "the attempt counts, or retries could not be bounded"

    assert service.claim("agent-2").id == task.id, "another agent can take it"


def test_heartbeat_keeps_the_task():
    task, _ = service.create({"title": "long"})
    service.claim("agent-1", lease_s=0.05)

    outcome, extended = service.heartbeat(task.id, "agent-1", lease_s=30)
    assert outcome is Outcome.updated

    time.sleep(0.08)
    assert service.reap_expired() == [], "the lease was renewed, nothing to collect"
    assert service.get(task.id).status.value == "in_progress"
    assert extended.lease_expires is not None


def test_heartbeat_refuses_a_foreign_task():
    task, _ = service.create({"title": "foreign"})
    service.claim("agent-1", lease_s=30)

    outcome, current = service.heartbeat(task.id, "agent-2", lease_s=30)
    assert outcome is Outcome.not_owner
    assert current.assignee_id == "agent-1"


def test_heartbeat_refuses_a_task_that_is_not_running():
    task, _ = service.create({"title": "still queued"})
    outcome, _ = service.heartbeat(task.id, "agent-1")
    assert outcome is Outcome.status_conflict


def test_claim_without_lease_is_never_reaped():
    """lease_s=0 refuses a lease outright: the task stays with its executor."""
    task, _ = service.create({"title": "no lease"})
    taken = service.claim("agent-1", lease_s=0)
    assert taken.lease_expires is None

    time.sleep(0.02)
    assert service.reap_expired() == []
    assert service.get(task.id).status.value == "in_progress"


def test_foreign_agent_cannot_close_someone_elses_work():
    task, _ = service.create({"title": "mine"})
    service.claim("agent-1", lease_s=30)

    outcome, current = service.set_status(task.id, "done", actor="agent-2")
    assert outcome is Outcome.not_owner
    assert current.status.value == "in_progress"

    outcome, done = service.set_status(task.id, "done", actor="agent-1")
    assert outcome is Outcome.updated
    assert done.status.value == "done"


def test_claim_without_lease_s_still_takes_a_lease():
    """A lease is taken by default, which is the point for a swarm — but it has
    to be known: an agent working past the term in silence loses the task."""
    service.create({"title": "naive agent"})
    taken = service.claim("agent-naive")
    assert taken.lease_expires is not None, "by default a task is leased, not held forever"
