"""The core serves the built dashboard and the chrome data behind it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from api.services import tasks as service


@pytest.fixture
def client():
    with TestClient(main.create_app()) as c:
        yield c


# The event stream is covered by test_events_contract.py: it is a public
# contract with a cursor now, not just a "something changed" ping.


def test_stats_carry_the_chrome_data(client):
    """One request is enough for the header: counters, projects, assignees."""
    service.create({"title": "одна"}, project="noted", assignee_id="agent-1")
    service.create({"title": "чужая"}, project="abot")

    payload = client.get("/api/stats").json()
    assert payload["stats"]["total"] == 2
    assert payload["projects"] == ["abot", "noted"]
    assert payload["assignees"] == ["agent-1"]

    scoped = client.get("/api/stats", params={"project": "noted"}).json()
    assert scoped["stats"]["total"] == 1, "counters are scoped to the project"


@pytest.mark.skipif(not (main.ui_dir() / "index.html").exists(), reason="дэшборд не собран")
def test_built_dashboard_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<div id="root">' in page.text
