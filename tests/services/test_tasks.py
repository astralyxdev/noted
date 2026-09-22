"""The task domain. No application is started: the service does not need FastAPI."""

from __future__ import annotations

import threading
import time

import pytest

from api.models.envelope import Outcome
from api.services import tasks as service
from api.services.tasks import TaskError
from tests.conftest import sqlite_only


def test_create_returns_pending_task():
    task, created = service.create({"title": "build the report"}, created_by="orchestrator")
    assert created is True
    assert task.status.value == "pending"
    assert task.task == {"title": "build the report"}
    assert task.created_by == "orchestrator"


def test_key_makes_create_idempotent():
    first, created_first = service.create({"title": "first"}, key="job-1")
    second, created_second = service.create({"title": "different text"}, key="job-1")

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.task == {"title": "first"}, "the existing task is not overwritten"
    assert len(service.list_tasks()) == 1


def test_claim_prefers_addressed_tasks_then_pool_in_fifo_order():
    pool_first, _ = service.create({"n": 1})
    addressed, _ = service.create({"n": 2}, assignee_id="agent-1")
    pool_second, _ = service.create({"n": 3})

    assert service.claim("agent-1").id == addressed.id
    assert service.claim("agent-1").id == pool_first.id
    assert service.claim("agent-1").id == pool_second.id
    assert service.claim("agent-1") is None


def test_claim_does_not_take_task_addressed_to_someone_else():
    service.create({"n": 1}, assignee_id="agent-1")
    assert service.claim("agent-2") is None


def test_compare_and_set_blocks_the_second_finisher():
    task, _ = service.create({"n": 1})
    service.claim("agent-1")

    outcome, updated = service.set_status(task.id, "done", result={"ok": True}, if_status="in_progress")
    assert outcome is Outcome.updated
    assert updated.result == {"ok": True}

    outcome, current = service.set_status(task.id, "failed", if_status="in_progress")
    assert outcome is Outcome.status_conflict
    assert current.status.value == "done", "a conflict returns the current state"


def test_set_status_reports_missing_task():
    outcome, task = service.set_status(999, "done")
    assert outcome is Outcome.not_found
    assert task is None


def test_result_is_kept_when_status_changes_without_it():
    task, _ = service.create({"n": 1})
    service.set_status(task.id, "failed", result={"error": "timeout"}, force=True)
    service.set_status(task.id, "pending", force=True)
    assert service.get(task.id).result == {"error": "timeout"}


def test_list_filters():
    done, _ = service.create({"n": 1})
    service.set_status(done.id, "done", force=True)
    pending, _ = service.create({"n": 2}, assignee_id="agent-1")
    pooled, _ = service.create({"n": 3})

    assert [t.id for t in service.list_tasks(status="pending")] == [pooled.id, pending.id]
    assert [t.id for t in service.list_tasks(status=["done", "pending"])][0] == pooled.id
    assert [t.id for t in service.list_tasks(assignee_id="agent-1")] == [pending.id]
    assert [t.id for t in service.list_tasks(unassigned=True)] == [pooled.id, done.id]
    assert service.list_tasks()[0].id == pooled.id, "newest first"


def test_list_omits_result_but_get_returns_it():
    task, _ = service.create({"n": 1})
    service.set_status(task.id, "done", result={"big": "payload"}, force=True)

    assert not hasattr(service.list_tasks()[0], "result")
    assert service.get(task.id).result == {"big": "payload"}


def test_stale_seconds_finds_hung_tasks():
    fresh, _ = service.create({"n": 1})
    stale, _ = service.create({"n": 2})
    service.claim("agent-1")

    with_lag = service.list_tasks(stale_seconds=0)
    assert {t.id for t in with_lag} == {fresh.id, stale.id}
    assert service.list_tasks(stale_seconds=3600) == []


def test_parent_must_exist():
    with pytest.raises(TaskError) as err:
        service.create({"n": 1}, parent_id=404)
    assert err.value.code is Outcome.parent_not_found


def test_bad_input_is_rejected():
    with pytest.raises(TaskError):
        service.create("not an object")
    with pytest.raises(TaskError):
        service.create({"n": 1}, assignee_id="   ")
    with pytest.raises(TaskError):
        service.set_status(1, "almost_done")


def test_concurrent_claims_never_hand_out_the_same_task():
    """The core invariant: however many agents claim at once, every task goes
    to exactly one of them."""
    total = 40
    for n in range(total):
        service.create({"n": n})

    claimed: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(8)

    def worker(name: str) -> None:
        start.wait()
        while True:
            task = service.claim(name)
            if task is None:
                return
            with guard:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(claimed) == total
    assert len(set(claimed)) == total, "one task went to two agents"
    assert service.stats()["in_progress"] == total


def test_project_scopes_the_list():
    service.create({"n": 1}, project="noted")
    service.create({"n": 2}, project="abot")
    service.create({"n": 3})

    assert [t.task["n"] for t in service.list_tasks(project="noted")] == [1]
    assert [t.task["n"] for t in service.list_tasks(unscoped=True)] == [3]
    assert service.projects() == ["abot", "noted"]


def test_claim_inside_a_project_never_reaches_outside_it():
    """The scope is strict: naming a project excludes both foreign and unscoped tasks."""
    service.create({"n": 1}, project="abot")
    service.create({"n": 2})

    assert service.claim("agent-1", project="noted") is None

    taken = service.claim("agent-1", project="abot")
    assert taken.task == {"n": 1}
    assert taken.project == "abot"

    assert service.claim("agent-1", project="abot") is None, "an unscoped task is not in the scope"
    assert service.claim("agent-1").task == {"n": 2}, "without a scope anything may be taken"


@sqlite_only
def test_old_database_gets_the_project_column(tmp_path, monkeypatch):
    """A database created before scopes existed must not fall apart.

    The migration by hand is a SQLite matter: PostgreSQL adds a missing column
    with IF NOT EXISTS and needs no PRAGMA to find out what is there."""
    import sqlite3

    from api.models import database

    old = tmp_path / "old.db"
    legacy = sqlite3.connect(old)
    legacy.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', assignee_id TEXT, created_by TEXT,
            parent_id INTEGER, key TEXT UNIQUE, result TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO tasks (task, status, created_at, updated_at)
        VALUES ('{"title": "old one"}', 'pending', 1, 1);
        """
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("NOTED_DB", str(old))
    database.close()

    existing = service.list_tasks()
    assert existing[0].task == {"title": "old one"}
    assert existing[0].project is None

    fresh, _ = service.create({"title": "new one"}, project="noted")
    assert fresh.project == "noted"


def test_cursor_walks_the_whole_list_without_gaps():
    """A cursor over id rather than an offset: new tasks keep arriving while
    somebody scrolls, and an offset would start skipping rows."""
    made = [service.create({"n": n})[0].id for n in range(10)]

    page, seen = service.list_tasks(limit=4), []
    while page:
        seen.extend(t.id for t in page)
        page = service.list_tasks(before_id=page[-1].id, limit=4)

    assert seen == sorted(made, reverse=True)

    # a task created mid-scroll does not shift what has already been shown
    first = service.list_tasks(limit=4)
    service.create({"n": 100})
    following = service.list_tasks(before_id=first[-1].id, limit=4)
    assert not {t.id for t in following} & {t.id for t in first}
