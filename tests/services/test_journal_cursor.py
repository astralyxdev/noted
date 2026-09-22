"""The journal cursor is a promise: everything, once, in order.

`/events` tells a client to remember the last id it saw and resume from there.
That only works if an entry can never appear *after* the cursor has moved past
its number — and ids come from a sequence, so they are handed out when a row is
written, not when it commits.
"""

from __future__ import annotations

import time

import pytest

from api.models import database
from api.services import tasks as service
from tests.conftest import TEST_DB_URL, postgres_only


def _hole_after(task_id: int) -> tuple[int, int]:
    """Write a journal entry whose number skips one, as an in-flight write would."""
    with database.transaction() as conn:
        top = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM task_events").fetchone()["n"]
        conn.execute(
            "INSERT INTO task_events (id, task_id, at, event, actor, to_status)"
            " VALUES (?, ?, ?, 'status', 'later', 'done')",
            (top + 2, task_id, time.time()),
        )
    return top, top + 2


def test_an_entry_beyond_a_hole_is_held_back():
    """Entry 11 is still in flight; entry 12 must wait for it, not overtake it."""
    task, _ = service.create({"title": "first"})
    before, beyond = _hole_after(task.id)

    delivered = [e.id for e in service.events_after(before, 50)]
    assert beyond not in delivered, "an entry was handed out over a hole, losing the one below it"


def test_a_hole_that_never_fills_does_not_block_the_stream_for_ever():
    """A rolled-back write leaves a number nothing will ever use. The stream
    has to step over it once it is clearly not coming."""
    task, _ = service.create({"title": "first"})
    before, beyond = _hole_after(task.id)

    # Age the entry past the grace period: whatever was missing below it has
    # been missing longer than that, so it is gone rather than slow.
    with database.transaction() as conn:
        conn.execute(
            "UPDATE task_events SET at = ? WHERE id = ?",
            (time.time() - service.GAP_GRACE_S - 1, beyond),
        )

    delivered = [e.id for e in service.events_after(before, 50)]
    assert beyond in delivered, "the stream stalled for ever on a hole that will never fill"


@postgres_only
def test_a_committed_entry_does_not_overtake_an_uncommitted_one():
    """The real race, with two transactions and a deliberate commit order."""
    import psycopg

    task, _ = service.create({"title": "subject"})
    start = service.last_event_id()

    slow = psycopg.connect(TEST_DB_URL)
    fast = psycopg.connect(TEST_DB_URL)
    try:
        # `slow` takes the lower number first and keeps its transaction open.
        slow.execute(
            "INSERT INTO task_events (task_id, at, event, actor, to_status)"
            " VALUES (%s, %s, 'status', 'slow', 'done')",
            (task.id, time.time()),
        )
        fast.execute(
            "INSERT INTO task_events (task_id, at, event, actor, to_status)"
            " VALUES (%s, %s, 'status', 'fast', 'done')",
            (task.id, time.time()),
        )
        fast.commit()  # the higher number becomes visible first

        seen = [e.actor for e in service.events_after(start, 50)]
        assert "fast" not in seen, "the later entry was delivered while the earlier one was still in flight"

        slow.commit()
        both = [e.actor for e in service.events_after(start, 50)]
        assert both == ["slow", "fast"], f"both entries must arrive, in order: {both}"
    finally:
        slow.close()
        fast.close()
