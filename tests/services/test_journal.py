"""Журнал переходов: по нему разбирают инцидент."""

from __future__ import annotations

import time

from api.services import tasks as service


def test_journal_records_the_whole_life_of_a_task():
    task, _ = service.create({"title": "путь"}, project="noted", created_by="orchestrator")
    service.claim("agent-1", lease_s=0.01)
    time.sleep(0.05)
    service.reap_expired()
    service.claim("agent-2", lease_s=30)
    service.set_status(task.id, "done", result={"ok": True}, actor="agent-2")

    log = service.events(task.id)
    assert [e.event for e in log] == ["created", "claimed", "reaped", "claimed", "status"]

    created, first_claim, reaped, second_claim, finished = log
    assert created.actor == "orchestrator"
    assert first_claim.actor == "agent-1"
    assert reaped.actor == "system"
    assert reaped.detail["held_by"] == "agent-1"
    assert second_claim.detail["attempt"] == 2
    assert finished.from_status.value == "in_progress"
    assert finished.to_status.value == "done"
    assert finished.actor == "agent-2"


def test_journal_is_append_only_per_task():
    one, _ = service.create({"title": "одна"})
    two, _ = service.create({"title": "другая"})
    service.set_status(one.id, "cancelled")

    assert [e.event for e in service.events(one.id)] == ["created", "status"]
    assert [e.event for e in service.events(two.id)] == ["created"]
