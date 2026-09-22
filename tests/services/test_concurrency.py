"""What has to hold when two callers arrive at once.

These run on whichever engine the suite is pointed at, and they are the reason
the PostgreSQL store takes locks by hand: SQLite serialises every writer behind
one process lock, so it passes these for free, while PostgreSQL runs the same
two requests side by side and only passes them because the SQL says so.
"""

from __future__ import annotations

import threading

from api.models.envelope import Outcome
from api.services import tasks as service
from api.models.task import Status


def _run(worker, count: int) -> list:
    """`count` threads released as close to together as the runtime allows."""
    results: list = [None] * count
    start = threading.Barrier(count)

    def wrapped(index: int) -> None:
        start.wait()
        results[index] = worker(index)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_every_claimer_gets_its_own_task():
    """Twenty agents, twenty tasks, one claim each: nobody duplicates and
    nobody comes back empty-handed while work is sitting in the queue.

    Coming back empty is the failure that a naive optimistic claim produces —
    two claimers pick the same row, one writes it, the other is told there is
    nothing. `FOR UPDATE SKIP LOCKED` makes the loser take the next row instead.
    """
    for n in range(20):
        service.create({"n": n})

    taken = _run(lambda i: service.claim(f"agent-{i}"), 20)

    assert all(t is not None for t in taken), "a claimer came back empty with work available"
    assert len({t.id for t in taken}) == 20, "two agents were handed the same task"


def test_only_one_writer_wins_a_compare_and_set():
    """Two agents report on the same task at once. Compare-and-set means
    exactly one of them changes it; the other is told what it really is."""
    task, _ = service.create({"title": "contested"})
    service.claim("agent-1")

    outcomes = _run(
        lambda i: service.set_status(task.id, Status.done, if_status=Status.in_progress, force=False)[0],
        8,
    )

    assert outcomes.count(Outcome.updated) == 1, f"expected one winner, got {outcomes}"
    assert all(o is Outcome.status_conflict for o in outcomes if o is not Outcome.updated)


def test_a_finished_task_is_not_reclaimed():
    """A claim and a close racing each other must not leave the task in
    progress with nobody on it."""
    task, _ = service.create({"title": "short"})
    service.claim("agent-1")

    def worker(index: int):
        if index == 0:
            return service.set_status(task.id, Status.done)[0]
        return service.claim(f"late-{index}")

    _run(worker, 6)

    after = service.get(task.id)
    assert after.status is Status.done
    assert after.assignee_id == "agent-1"
