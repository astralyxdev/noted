"""Retries and the dead letter."""

from __future__ import annotations

import time

import pytest

from api.services import tasks as service


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Fractions of a second between attempts, or the test would wait minutes."""
    monkeypatch.setenv("NOTED_RETRY_BASE_S", "0.02")
    monkeypatch.setenv("NOTED_RETRY_CAP_S", "0.05")


def test_failure_returns_to_the_queue_until_attempts_run_out():
    task, _ = service.create({"title": "flaky"}, max_attempts=2)

    service.claim("agent-1")
    outcome, after_first = service.set_status(task.id, "failed", result={"error": "502"})
    assert after_first.status.value == "pending", "attempts remain, so it is queued again"
    assert after_first.attempts == 1
    assert after_first.retry_after is not None

    assert service.claim("agent-1") is None, "not handed out before the pause is over"
    time.sleep(0.05)

    second = service.claim("agent-1")
    assert second.id == task.id
    assert second.attempts == 2

    _, dead = service.set_status(task.id, "failed", result={"error": "502 again"})
    assert dead.status.value == "failed", "attempts exhausted, so it stays failed"
    assert service.claim("agent-1") is None


def test_task_without_max_attempts_fails_immediately():
    task, _ = service.create({"title": "one-shot"})
    service.claim("agent-1")
    _, failed = service.set_status(task.id, "failed")
    assert failed.status.value == "failed"


def test_expired_lease_with_no_attempts_left_goes_to_dead_letter():
    task, _ = service.create({"title": "vanishing agent"}, max_attempts=1)
    service.claim("agent-1", lease_s=0.01)

    time.sleep(0.05)
    assert service.reap_expired() == [], "nothing to resurrect, that was the last attempt"

    dead = service.get(task.id)
    assert dead.status.value == "failed"
    assert dead.result == {"error": "lease expired, attempts exhausted"}
    assert [e.event for e in service.events(task.id)][-1] == "dead_letter"
