"""Dependencies and priority: what is handed out, and in what order."""

from __future__ import annotations

import pytest

from api.services import tasks as service
from api.services.tasks import TaskError


def test_task_waits_for_its_dependencies():
    first, _ = service.create({"title": "сначала"})
    second, _ = service.create({"title": "потом"}, depends_on=[first.id])

    assert service.get(second.id).depends_on == [first.id]

    taken = service.claim("agent-1")
    assert taken.id == first.id, "a task with an open dependency is not handed out"
    assert service.claim("agent-2") is None

    service.set_status(first.id, "done")
    assert service.claim("agent-2").id == second.id


def test_failed_dependency_blocks_and_success_unblocks():
    first, _ = service.create({"title": "сначала"})
    second, _ = service.create({"title": "потом"}, depends_on=[first.id])

    service.claim("agent-1")
    service.set_status(first.id, "failed")
    assert service.get(second.id).status.value == "blocked", "nothing left to wait for, and it shows"

    # Manual intervention: a human reopened the failed task and finished it.
    service.set_status(first.id, "done", force=True)
    assert service.get(second.id).status.value == "pending"
    assert service.claim("agent-2").id == second.id


def test_dependency_must_exist():
    with pytest.raises(TaskError):
        service.create({"title": "висячая"}, depends_on=[404])


def test_priority_beats_fifo_inside_the_pool():
    low, _ = service.create({"title": "обычная"})
    high, _ = service.create({"title": "срочная"}, priority=10)
    middle, _ = service.create({"title": "заметная"}, priority=5)

    assert [service.claim("agent-1").id for _ in range(3)] == [high.id, middle.id, low.id]


def test_dispatch_order_is_priority_then_addressing_then_fifo():
    """Dispatch order is a contract. Priority comes first: otherwise a trifle
    addressed to me personally would overtake urgent work from the pool."""
    urgent, _ = service.create({"title": "срочная из пула"}, priority=99)
    mine, _ = service.create({"title": "моя обычная"}, assignee_id="agent-1")
    pooled, _ = service.create({"title": "обычная из пула"})

    assert [service.claim("agent-1").id for _ in range(3)] == [urgent.id, mine.id, pooled.id]


def test_list_shows_how_many_dependencies_are_still_open():
    """A pending task nobody can take must be tellable apart at a glance."""
    first, _ = service.create({"title": "сначала"})
    second, _ = service.create({"title": "потом"}, depends_on=[first.id])

    waiting = {t.id: t.waiting_on for t in service.list_tasks()}
    assert waiting[second.id] == 1
    assert waiting[first.id] == 0

    service.claim("agent-1")
    service.set_status(first.id, "done")
    assert {t.id: t.waiting_on for t in service.list_tasks()}[second.id] == 0
