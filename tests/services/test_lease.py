"""Аренда, heartbeat и автоматический возврат задачи в очередь.

Это то, что отличает «видно, что залипло» от «само починилось»: без аренды
упавший агент оставляет задачу висеть в in_progress навсегда.
"""

from __future__ import annotations

import time

from api.models.envelope import Outcome
from api.services import tasks as service


def test_expired_lease_returns_the_task_to_the_queue():
    task, _ = service.create({"title": "долгая"})
    taken = service.claim("agent-1", lease_s=0.01)
    assert taken.id == task.id
    assert taken.lease_expires is not None

    time.sleep(0.05)
    assert service.reap_expired() == [task.id]

    back = service.get(task.id)
    assert back.status.value == "pending"
    assert back.assignee_id is None, "задача вернулась в общий пул, а не осталась за мёртвым агентом"
    assert back.attempts == 1, "попытка засчитана — иначе повторы не ограничить"

    assert service.claim("agent-2").id == task.id, "её может взять другой агент"


def test_heartbeat_keeps_the_task():
    task, _ = service.create({"title": "долгая"})
    service.claim("agent-1", lease_s=0.05)

    outcome, extended = service.heartbeat(task.id, "agent-1", lease_s=30)
    assert outcome is Outcome.updated

    time.sleep(0.08)
    assert service.reap_expired() == [], "аренда продлена — забирать нечего"
    assert service.get(task.id).status.value == "in_progress"
    assert extended.lease_expires is not None


def test_heartbeat_refuses_a_foreign_task():
    task, _ = service.create({"title": "чужая"})
    service.claim("agent-1", lease_s=30)

    outcome, current = service.heartbeat(task.id, "agent-2", lease_s=30)
    assert outcome is Outcome.not_owner
    assert current.assignee_id == "agent-1"


def test_heartbeat_refuses_a_task_that_is_not_running():
    task, _ = service.create({"title": "ещё в очереди"})
    outcome, _ = service.heartbeat(task.id, "agent-1")
    assert outcome is Outcome.status_conflict


def test_claim_without_lease_is_never_reaped():
    """lease_s=0 — явный отказ от аренды: задача остаётся за исполнителем."""
    task, _ = service.create({"title": "без аренды"})
    taken = service.claim("agent-1", lease_s=0)
    assert taken.lease_expires is None

    time.sleep(0.02)
    assert service.reap_expired() == []
    assert service.get(task.id).status.value == "in_progress"


def test_foreign_agent_cannot_close_someone_elses_work():
    task, _ = service.create({"title": "моя"})
    service.claim("agent-1", lease_s=30)

    outcome, current = service.set_status(task.id, "done", actor="agent-2")
    assert outcome is Outcome.not_owner
    assert current.status.value == "in_progress"

    outcome, done = service.set_status(task.id, "done", actor="agent-1")
    assert outcome is Outcome.updated
    assert done.status.value == "done"
