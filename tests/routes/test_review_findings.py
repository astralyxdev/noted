"""Regressions for the holes found in review.

Every case here once passed through. They are grouped in one file on purpose:
each is a place where safety depended on what a client chose to send, rather
than on what the server demanded.
"""

from __future__ import annotations

import httpx
import pytest

from api.models.agent import SELF_RENEWING
from api.services import agents
from api.services import tasks as service
from api.services.agents import AgentError


@pytest.fixture
def keys(monkeypatch):
    """Two scoped agents in different projects."""
    monkeypatch.setenv("NOTED_TOKEN", "admin-secret")
    return {
        "a": agents.issue("agent-a", projects=["pa"]).key,
        "b": agents.issue("agent-b", projects=["pb"]).key,
    }


def _client(live_server, key, **headers):
    return httpx.Client(base_url=live_server, headers={"X-Noted-Token": key, **headers}, timeout=10)


def test_scope_governs_reading_not_only_writing(live_server, keys):
    """A scope that only guards the way in is advisory, whatever the docs say."""
    mine = service.create({"title": "theirs"}, project="pa")[0]

    with _client(live_server, keys["b"]) as b:
        listed = b.get("/api/tasks").json()
        assert all(t["project"] != "pa" for t in listed["tasks"]), "a foreign project must not be listed"

        assert b.get(f"/api/tasks/{mine.id}").status_code == 403
        assert b.get(f"/api/tasks/{mine.id}/events").status_code == 403

        denied = b.patch(f"/api/tasks/{mine.id}/status", json={"status": "cancelled", "if_status": "pending"})
        assert denied.status_code == 403
        assert service.get(mine.id).status.value == "pending"


def test_force_is_not_available_to_an_agent(live_server, keys):
    """force is a human overriding the queue. An agent reaching for it would
    walk around compare-and-set and unblock dependants of a task it never held."""
    blocker = service.create({"title": "blocker"}, project="pb")[0]
    waiting = service.create({"title": "waiting"}, project="pb", depends_on=[blocker.id])[0]

    with _client(live_server, keys["b"]) as b:
        forced = b.patch(f"/api/tasks/{blocker.id}/status", json={"status": "done", "force": True})
        assert forced.status_code == 403
        assert forced.json()["outcome"] == "forbidden"

    assert service.get(blocker.id).status.value == "pending"
    assert service.get(waiting.id).waiting_on == 1, "the dependant stayed blocked"


def test_fencing_cannot_be_dropped_by_omitting_the_header(live_server, keys):
    """Sending no session used to skip the check entirely — the same mistake
    compare-and-set had before it became the default."""
    task = service.create({"title": "held"}, project="pb")[0]

    with _client(live_server, keys["b"], **{"X-Noted-Session": "instance-one"}) as first:
        first.post("/api/tasks/claim", json={"assignee_id": "agent-b"})

    with _client(live_server, keys["b"]) as bare:
        refused = bare.patch(f"/api/tasks/{task.id}/status", json={"status": "done"})
        assert refused.status_code == 409
        assert refused.json()["outcome"] == "stale_session"

    assert service.get(task.id).status.value == "in_progress"


def test_a_session_id_belongs_to_one_agent(live_server, keys):
    """Session ids travel in headers. Presenting somebody else's must fail, or
    an agent could hold another's tasks for as long as it likes."""
    with _client(live_server, keys["a"], **{"X-Noted-Session": "session-of-a"}) as a:
        assert a.get("/api/tasks").status_code == 200

    with _client(live_server, keys["b"], **{"X-Noted-Session": "session-of-a"}) as b:
        assert b.get("/api/tasks").status_code == 401


def test_a_closed_session_stays_closed(live_server, keys):
    """Reopening one would give a revoked key, or a process that already died,
    its tasks back."""
    with _client(live_server, keys["a"], **{"X-Noted-Session": "session-of-a"}) as a:
        assert a.get("/api/tasks").status_code == 200

        agents.close_session("session-of-a")
        assert a.get("/api/tasks").status_code == 401


def test_the_event_stream_is_behind_the_same_door(live_server, keys):
    """The journal carries actors, projects and payload details."""
    with httpx.Client(base_url=live_server, timeout=10) as anonymous:
        assert anonymous.get("/events", params={"after": 0}).status_code == 401


def test_issuing_the_first_key_cannot_lock_the_dashboard_out(monkeypatch):
    """Identity on with no admin credential would leave the browser with 401 on
    every request and no way to sign in."""
    monkeypatch.delenv("NOTED_TOKEN", raising=False)

    with pytest.raises(AgentError) as err:
        agents.issue("agent-1", projects=["pa"])
    assert "NOTED_TOKEN" in err.value.message

    # An admin key is the other way in, and it opens the dashboard door.
    admin = agents.issue("root", is_admin=True)
    agents.issue("agent-1", projects=["pa"])
    assert agents.resolve(admin.key).is_admin is True


def test_a_retry_releases_the_session(monkeypatch):
    """A pending task holding a stale session would answer stale_session to the
    very agent that picks it up next."""
    monkeypatch.setenv("NOTED_RETRY_BASE_S", "0.01")
    task, _ = service.create({"title": "flaky"}, max_attempts=3)

    first = agents.open_session("agent-1", SELF_RENEWING)
    service.claim("agent-1", session_id=first.id)
    service.set_status(task.id, "failed", actor="agent-1", session_id=first.id, strict_session=True)

    import time

    time.sleep(0.05)
    second = agents.open_session("agent-2", SELF_RENEWING)
    retried = service.claim("agent-2", session_id=second.id)
    assert retried.id == task.id

    outcome, _ = service.set_status(task.id, "done", actor="agent-2", session_id=second.id, strict_session=True)
    assert outcome.value == "updated", "the new holder is not fenced out by the old session"


def test_blocking_walks_the_whole_chain():
    """C depends on B, B on A. With A failed, C used to sit in pending for ever:
    never handed out, never shown as blocked."""
    a, _ = service.create({"title": "a"})
    b, _ = service.create({"title": "b"}, depends_on=[a.id])
    c, _ = service.create({"title": "c"}, depends_on=[b.id])

    service.claim("agent-1")
    service.set_status(a.id, "failed", actor="agent-1")

    assert service.get(b.id).status.value == "blocked"
    assert service.get(c.id).status.value == "blocked", "the far end of the chain is blocked too"

    service.set_status(a.id, "done", force=True)
    assert service.get(b.id).status.value == "pending"
    assert service.get(c.id).status.value == "blocked", "C waits until B itself is done"


def test_the_claim_doc_does_not_promise_a_leaseless_session():
    """The tool description is the only spec most agents ever read.

    It used to say that a session meant no lease was set. The code has always
    taken one, and an agent that believed the description would plan its
    heartbeats around a guarantee that was not there. Pinned, because this
    exact drift has happened twice.
    """
    from api.services import tasks as service
    from api.utils.tool_docs import CLAIM_TASK

    task, _ = service.create({"title": "leased"})
    taken = service.claim("agent-1", session_id="a-live-session")
    assert taken.lease_expires is not None, "a claim inside a session still takes a lease"

    assert "no lease is set" not in CLAIM_TASK
    assert "second guard" in CLAIM_TASK, "the doc must describe the lease and session as both holding"


def test_nothing_still_refers_to_the_removed_stdio_adapter():
    """The adapter is gone; the tools must not send agents looking for it."""
    from api.utils import tool_docs

    for name in ("SET_TASK", "GET_TASKS", "GET_TASK", "SET_STATUS", "CLAIM_TASK", "HEARTBEAT"):
        assert "stdio" not in getattr(tool_docs, name).lower(), f"{name} still mentions stdio"


#: Identifiers that were removed from the project. A document still naming one
#: sends a reader after something that is not there.
GONE = ("mcp_adapter", "noted-mcp", "NOTED_API", "NOTED_KEY", "api_unavailable")

#: The one passage allowed to name them: the note explaining the removal.
REMOVAL_NOTE = "A stdio adapter existed and was removed"


def test_no_document_points_at_something_that_was_deleted():
    """Prose drifts more quietly than code, and nothing compiles it.

    Every one of these was found stale by review after the stdio adapter was
    removed: file-tree entries for `client.py` and `server.py`, a test that no
    longer exists, and three sentences about "the adapter" calling the same
    code as the routes.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("README.md", "SPEC.md", "TECHNICAL_DOCUMENTATION.md", "RECIPES.md", ".env.example"):
        for number, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
            if REMOVAL_NOTE in line:
                continue
            for dead in GONE:
                assert dead not in line, f"{name}:{number} still refers to {dead}: {line.strip()}"


def test_the_readme_settings_table_matches_the_code():
    """Every documented variable is read, and every variable read is documented.

    A default that drifts is worse than one that is missing: it is believed.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = (root / "api" / "settings.py").read_text(encoding="utf-8")
    documented = set(re.findall(r"\| `(NOTED_[A-Z_]+)`", (root / "README.md").read_text(encoding="utf-8")))
    # The table writes a pair as `NOTED_A` / `NOTED_B`, so pick those up too.
    documented |= set(re.findall(r"`(NOTED_[A-Z_]+)`", (root / "README.md").read_text(encoding="utf-8")))
    read_by_code = set(re.findall(r'"(NOTED_[A-Z_]+)"', code))

    assert read_by_code - documented == set(), f"read but undocumented: {read_by_code - documented}"
    assert documented - read_by_code == set(), f"documented but never read: {documented - read_by_code}"
