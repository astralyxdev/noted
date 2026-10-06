"""The agent skill is served with this server's own address written into it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    with TestClient(main.create_app()) as c:
        yield c


def test_skill_is_served_as_markdown(client):
    page = client.get("/skill.md")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/markdown")
    assert page.text.startswith("---\nname: noted\n"), "the frontmatter makes it installable as a skill"


def test_skill_carries_the_address_it_was_fetched_from(client):
    text = client.get("/skill.md").text
    assert "{{BASE_URL}}" not in text
    assert "claude mcp add --transport http noted http://testserver/mcp/" in text


def test_skill_is_not_shadowed_by_the_dashboard(client):
    """The dashboard takes the root; /skill.md must still reach the route."""
    assert "<div id=\"root\">" not in client.get("/skill.md").text


def test_skill_answers_head(client):
    """Fetchers check with HEAD before they download."""
    assert client.head("/skill.md").status_code == 200
