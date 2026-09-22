"""Compare-and-set is on by default.

For a primitive, the dangerous behaviour has to be an explicit opt-out rather
than a forgotten opt-in: an agent that omits if_status must not break
invariants in silence.
"""

from __future__ import annotations

from api.models.envelope import Outcome
from api.services import tasks as service


def test_forgotten_if_status_no_longer_breaks_invariants():
    task, _ = service.create({"title": "closed"})
    service.claim("agent-1")
    service.set_status(task.id, "done")

    # A late agent writes done a second time, having forgotten if_status.
    outcome, current = service.set_status(task.id, "done")
    assert outcome is Outcome.status_conflict
    assert current.status.value == "done", "done to done does not slip through"

    # Nor can a closed task be dropped back into the queue by accident.
    outcome, _ = service.set_status(task.id, "pending")
    assert outcome is Outcome.status_conflict


def test_executor_reports_from_in_progress_without_saying_so():
    """The usual executor path needs no extra words: it is in_progress already."""
    task, _ = service.create({"title": "ordinary"})
    service.claim("agent-1")

    outcome, done = service.set_status(task.id, "done", result={"ok": True})
    assert outcome is Outcome.updated
    assert done.status.value == "done"


def test_force_is_the_explicit_way_to_write_unconditionally():
    """A human cancels a task in any state from the dashboard — but says so."""
    task, _ = service.create({"title": "queued"})

    outcome, _ = service.set_status(task.id, "cancelled")
    assert outcome is Outcome.status_conflict, "no unconditional write by default"

    outcome, cancelled = service.set_status(task.id, "cancelled", force=True)
    assert outcome is Outcome.updated
    assert cancelled.status.value == "cancelled"


def test_explicit_if_status_still_wins():
    task, _ = service.create({"title": "an own check"})
    outcome, _ = service.set_status(task.id, "blocked", if_status="pending")
    assert outcome is Outcome.updated


def test_finishing_releases_the_session_so_latecomers_cannot_overwrite():
    """A human cancelled the task; a returning agent must not overwrite that."""
    from api.models.agent import SELF_RENEWING
    from api.services import agents

    task, _ = service.create({"title": "contested"})
    session = agents.open_session("agent-1", SELF_RENEWING)
    service.claim("agent-1", session_id=session.id)

    service.set_status(task.id, "cancelled", force=True)

    outcome, _ = service.set_status(task.id, "done", actor="agent-1", session_id=session.id, strict_session=True)
    assert outcome is Outcome.status_conflict, "the cancellation is not overwritten"
    assert service.get(task.id).status.value == "cancelled"
