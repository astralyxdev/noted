"""One test per defect found in review, so a fixed bug cannot come back quietly.

What is not here any more went out with the identity layer: agents authenticate
nowhere in Noted, so there are no keys, scopes, sessions or fencing left to
regress. Ids come from whatever runs the agents.
"""

from __future__ import annotations

from api.services import tasks as service


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


def test_a_claim_always_takes_a_lease_and_the_doc_says_so():
    """The tool description is the only spec most agents ever read.

    It used to promise that a task could be held without a lease. The code has
    always taken one, and an agent that believed the description would plan its
    heartbeats around a guarantee that was not there.
    """
    from api.utils.tool_docs import CLAIM_TASK

    task, _ = service.create({"title": "leased"})
    taken = service.claim("agent-1")
    assert taken.lease_expires is not None, "a claim takes a lease"

    opted_out = service.create({"title": "unbounded"})[0]
    service.set_status(task.id, "done", actor="agent-1")
    assert service.claim("agent-1", lease_s=0).lease_expires is None, "lease_s=0 is the opt-out"
    assert opted_out is not None

    assert "no lease is set" not in CLAIM_TASK
    assert "lease_s=0" in CLAIM_TASK, "the opt-out has to be documented, since it is the only one"


def test_a_health_check_that_checks_nothing_is_not_a_health_check(monkeypatch):
    """With the database unreachable the container reported healthy for ever —
    and so did anything waiting on `service_healthy` — while every real request
    failed."""
    from fastapi.testclient import TestClient

    import main
    from api.models import database

    with TestClient(main.create_app()) as client:
        assert client.get("/healthz").json()["outcome"] == "ok"

        def broken() -> None:
            raise RuntimeError("the store is gone")

        monkeypatch.setattr(database, "ping", broken)
        answer = client.get("/healthz")
        assert answer.status_code != 200, "a dead store still reported healthy"
        assert answer.json()["outcome"] == "internal_error"


#: Identifiers that were removed from the project. A document still naming one
#: sends a reader after something that is not there.
GONE = (
    "mcp_adapter", "noted-mcp", "NOTED_API", "NOTED_KEY", "api_unavailable",
    "noted-keys", "NOTED_TOKEN", "X-Noted-Token", "X-Noted-Session",
    "X-Noted-Transport", "NOTED_SESSION_TTL_S", "stale_session", "agent_sessions",
    "/api/login", "/api/logout", "sessions/renew", "self-renewing", "--admin",
)

#: Behaviour that was removed. Identifiers are the easy half: prose describing
#: a mechanism that no longer exists passes every spelling check, and the first
#: version of this test let four such passages through — a README promising
#: that "the transport renews the session" is worse than a stale file path,
#: because somebody will plan their heartbeats around it.
GONE_BEHAVIOUR = (
    "fencing",
    "renews the session",
    "renews its session",
    "both guards",
    "either guard",
    "agent-plus-session",
    "is the one exception",
    "behind the same door",
    "scoped key",
    "scoped agent",
    "admin key",
)

#: `session` alone cannot be banned: the MCP transport really does have a
#: session manager. These are the words that make an occurrence legitimate.
SESSION_IS_FINE = ("session manager", "session_manager", "streamable", "Mcp-Session", "no sessions")

#: The passages allowed to name what was removed: the notes explaining it.
ALLOWED = ("A stdio adapter existed and was removed", "authenticates nobody")


def test_nothing_written_for_a_reader_points_at_something_deleted():
    """Prose drifts more quietly than code, and nothing compiles it.

    The dashboard is included because it tells people how to connect, and it
    went on advertising the stdio adapter for a while after the adapter was
    deleted — the first version of this test only looked at the docs.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    written = ["README.md", "SPEC.md", "TECHNICAL_DOCUMENTATION.md", "RECIPES.md", ".env.example"]
    written += [str(f.relative_to(root)) for f in (root / "dashboard" / "src").rglob("*.tsx")]
    for name in written:
        for number, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
            if any(note in line for note in ALLOWED):
                continue
            for dead in GONE:
                assert dead not in line, f"{name}:{number} still refers to {dead}: {line.strip()}"
            for dead in GONE_BEHAVIOUR:
                assert dead not in line.lower(), (
                    f"{name}:{number} still describes {dead!r}, which the code no longer does: {line.strip()}"
                )
            if "session" in line.lower() and not any(ok.lower() in line.lower() for ok in SESSION_IS_FINE):
                raise AssertionError(f"{name}:{number} mentions a session; there are none: {line.strip()}")


def test_the_readme_settings_table_matches_the_code():
    """Every documented variable is read, and every variable read is documented.

    A default that drifts is worse than one that is missing: it is believed.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = (root / "api" / "settings.py").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(NOTED_[A-Z_]+)`", readme))
    read_by_code = set(re.findall(r'"(NOTED_[A-Z_]+)"', code))

    assert read_by_code, "the settings module reads nothing — the check would pass vacuously"
    assert read_by_code - documented == set(), f"read but undocumented: {read_by_code - documented}"
    assert documented - read_by_code == set(), f"documented but never read: {documented - read_by_code}"


def test_every_test_file_the_documentation_lists_exists():
    """The test list drifted twice: it named files that had been deleted."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    doc = (root / "TECHNICAL_DOCUMENTATION.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"`((?:services|routes|mcp)/test_[a-z_]+\.py)`", doc))

    assert listed, "nothing was found to check — the pattern stopped matching"
    missing = {name for name in listed if not (root / "tests" / name).exists()}
    assert not missing, f"the documentation lists tests that do not exist: {sorted(missing)}"


def test_every_path_in_the_readme_api_table_is_a_real_route():
    """The table listed three endpoints that had been deleted.

    Checked against the schema the application publishes, plus the mounts that
    never reach it (`/mcp/`, and `/healthz`, which is deliberately out of the
    schema). Requesting the paths instead would be a better test of reality and
    a worse one of patience: `/events` is a stream that never ends.

    `{id}` in the table is compared by shape, not by name — the route calls it
    `{task_id}`, and that is presentation rather than drift.
    """
    import re
    from pathlib import Path

    import main

    def shape(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/")) or "/"

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    table = re.search(r"## HTTP API\n(.*?)\n\n", readme, re.S)
    assert table, "the HTTP API table is gone from the README"

    documented = {shape(m) for m in re.findall(r"\| `(/[^`]*)` \|", table.group(1))}
    assert documented, "no paths were parsed — the table's shape changed and this check went blind"

    app = main.create_app()
    real = {shape(path) for path in app.openapi()["paths"]}
    real |= {shape(getattr(route, "path", "")) for route in app.routes if getattr(route, "path", None)}

    missing = documented - real
    assert not missing, f"the README documents routes that do not exist: {sorted(missing)}"
    assert len(documented) > 5, "the table parsed suspiciously small; check the pattern"


#: Words that would only appear in prose describing the removed identity layer.
#: `renew` is the awkward one: renewing a *lease* is real and current, so a line
#: may use it only while saying so.
BANNED = {
    "login": (),
    "logout": (),
    "sign in": (),
    "sessions/renew": (),
    "renew": ("lease",),
    "session": ("session manager", "session_manager", "streamable", "mcp-session", "no sessions"),
}


def test_the_documentation_does_not_speak_of_sessions_renewal_or_signing_in():
    """A blunt grep, kept blunt on purpose.

    Every one of these words described a mechanism that was deleted, and prose
    describing a deleted mechanism reads exactly like prose describing a live
    one. A reader who believes "the transport renews the session" skips
    `heartbeat` and loses the task five minutes later.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("README.md", "SPEC.md", "TECHNICAL_DOCUMENTATION.md", "RECIPES.md", ".env.example"):
        for number, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            if any(note.lower() in lowered for note in ALLOWED):
                continue
            for word, excuses in BANNED.items():
                if word in lowered and not any(excuse in lowered for excuse in excuses):
                    raise AssertionError(f"{name}:{number} says {word!r}: {line.strip()}")
