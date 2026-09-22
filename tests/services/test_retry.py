"""Повторы и dead letter."""

from __future__ import annotations

import time

import pytest

from api.services import tasks as service


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Пауза между попытками — доли секунды, иначе тест ждёт минутами."""
    monkeypatch.setenv("NOTED_RETRY_BASE_S", "0.02")
    monkeypatch.setenv("NOTED_RETRY_CAP_S", "0.05")


def test_failure_returns_to_the_queue_until_attempts_run_out():
    task, _ = service.create({"title": "шаткая"}, max_attempts=2)

    service.claim("agent-1")
    outcome, after_first = service.set_status(task.id, "failed", result={"error": "502"})
    assert after_first.status.value == "pending", "попытки остались — задача снова в очереди"
    assert after_first.attempts == 1
    assert after_first.retry_after is not None

    assert service.claim("agent-1") is None, "до истечения паузы задача не выдаётся"
    time.sleep(0.05)

    second = service.claim("agent-1")
    assert second.id == task.id
    assert second.attempts == 2

    _, dead = service.set_status(task.id, "failed", result={"error": "502 снова"})
    assert dead.status.value == "failed", "попытки исчерпаны — задача остаётся проваленной"
    assert service.claim("agent-1") is None


def test_task_without_max_attempts_fails_immediately():
    task, _ = service.create({"title": "одноразовая"})
    service.claim("agent-1")
    _, failed = service.set_status(task.id, "failed")
    assert failed.status.value == "failed"


def test_expired_lease_with_no_attempts_left_goes_to_dead_letter():
    task, _ = service.create({"title": "исчезающий агент"}, max_attempts=1)
    service.claim("agent-1", lease_s=0.01)

    time.sleep(0.05)
    assert service.reap_expired() == [], "воскрешать нечего — попытка была последней"

    dead = service.get(task.id)
    assert dead.status.value == "failed"
    assert dead.result == {"error": "аренда истекла, попытки исчерпаны"}
    assert [e.event for e in service.events(task.id)][-1] == "dead_letter"
