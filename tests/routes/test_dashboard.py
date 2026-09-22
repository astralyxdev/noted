"""Ядро отдаёт собранный дэшборд и поток событий."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from api.services import tasks as service


@pytest.fixture
def client():
    with TestClient(main.create_app()) as c:
        yield c


def test_sse_stream_announces_changes(live_server):
    """Дэшборд узнаёт об изменениях от сервера, а не опросом по таймеру."""
    with httpx.Client(base_url=live_server, timeout=15) as client:
        with client.stream("GET", "/events") as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")

            lines = stream.iter_lines()
            assert "retry:" in next(lines)

            httpx.post(f"{live_server}/api/tasks", json={"task": {"title": "разбуди дэшборд"}}, timeout=5)

            seen = ""
            for _ in range(8):
                seen += next(lines)
                if "event: tasks" in seen:
                    return
            raise AssertionError(f"событие не пришло: {seen!r}")


def test_stats_carry_the_chrome_data(client):
    """Шапке дэшборда хватает одного запроса: счётчики, проекты, исполнители."""
    service.create({"title": "одна"}, project="noted", assignee_id="agent-1")
    service.create({"title": "чужая"}, project="abot")

    payload = client.get("/api/stats").json()
    assert payload["stats"]["total"] == 2
    assert payload["projects"] == ["abot", "noted"]
    assert payload["assignees"] == ["agent-1"]

    scoped = client.get("/api/stats", params={"project": "noted"}).json()
    assert scoped["stats"]["total"] == 1, "счётчики считаются внутри проекта"


@pytest.mark.skipif(not (main.ui_dir() / "index.html").exists(), reason="дэшборд не собран")
def test_built_dashboard_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<div id="root">' in page.text
