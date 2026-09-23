from __future__ import annotations

import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from api.models import database
from api.utils import events


#: Point this at a PostgreSQL DSN to run the whole suite against that engine
#: instead of SQLite. The tests themselves know nothing about it — which is the
#: point: the same behaviour has to hold on both stores.
TEST_DB_URL = (os.environ.get("NOTED_TEST_DB_URL") or "").strip()

postgres_only = pytest.mark.skipif(not TEST_DB_URL, reason="needs NOTED_TEST_DB_URL")
sqlite_only = pytest.mark.skipif(bool(TEST_DB_URL), reason="SQLite-specific")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """A fresh database and clean Conditions per test, so tests never see each other."""
    if TEST_DB_URL:
        # The pool is kept between tests and the tables are emptied instead:
        # reconnecting per test costs more than the whole suite.
        monkeypatch.setenv("NOTED_DB_URL", TEST_DB_URL)
        database.wipe()
        events.reset()
        yield
        return
    monkeypatch.delenv("NOTED_DB_URL", raising=False)
    monkeypatch.setenv("NOTED_DB", str(tmp_path / "tasks.db"))
    database.close()
    events.reset()
    yield
    database.close()


@pytest.fixture(scope="session", autouse=True)
def close_the_store():
    """A PostgreSQL pool runs threads of its own; pytest must not exit around
    them, or the run ends in a page of warnings it cannot act on."""
    yield
    database.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch):
    """A real uvicorn on its own port.

    Needed wherever the in-memory ASGI transport will not do: streaming
    responses (SSE), and the MCP client, which must speak real HTTP.
    """
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app_factory, factory=True, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("the core did not come up")

    yield base
    server.should_exit = True
    thread.join(timeout=10)


def app_factory():
    import main

    return main.create_app()
