"""The guard rail on task creation.

The queue is open, so anyone may post to it. That means a looping agent can
flood it with a thousand tasks in seconds, and stopping it is the service's job.
"""

from __future__ import annotations

import pytest

from api.models.envelope import Outcome
from api.services import tasks as service
from api.services.tasks import TaskError


@pytest.fixture(autouse=True)
def tight_limit(monkeypatch):
    monkeypatch.setenv("NOTED_CREATE_LIMIT", "3")
    monkeypatch.setenv("NOTED_CREATE_WINDOW_S", "60")


def test_runaway_author_is_stopped():
    for n in range(3):
        service.create({"n": n}, created_by="agent-loop")

    with pytest.raises(TaskError) as err:
        service.create({"n": 99}, created_by="agent-loop")

    assert err.value.code is Outcome.rate_limited
    assert "притормозите" in err.value.message
    assert len(service.list_tasks()) == 3, "the extra task was not created"


def test_authors_have_separate_budgets():
    for n in range(3):
        service.create({"n": n}, created_by="agent-loop")

    fine, created = service.create({"title": "чужая работа"}, created_by="agent-other")
    assert created is True
    assert fine.created_by == "agent-other"


def test_anonymous_creators_share_one_budget():
    """Everyone without an author shares one bucket, or an empty field would bypass the guard."""
    for n in range(3):
        service.create({"n": n})

    with pytest.raises(TaskError):
        service.create({"n": 99})


def test_idempotent_repeat_is_not_throttled():
    """A repeat under the same key creates nothing, so there is nothing to cut."""
    first, _ = service.create({"n": 1}, created_by="agent-loop", key="job-1")
    service.create({"n": 2}, created_by="agent-loop")
    service.create({"n": 3}, created_by="agent-loop")

    again, created = service.create({"n": 1}, created_by="agent-loop", key="job-1")
    assert created is False
    assert again.id == first.id


def test_limit_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("NOTED_CREATE_LIMIT", "0")
    for n in range(20):
        service.create({"n": n}, created_by="agent-loop")
    assert len(service.list_tasks(limit=100)) == 20
