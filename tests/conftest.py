from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from api.models import database
from api.utils import events


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Своя база на каждый тест и чистая Condition: тесты не видят друг друга."""
    monkeypatch.setenv("NOTED_DB", str(tmp_path / "tasks.db"))
    monkeypatch.delenv("NOTED_TOKEN", raising=False)
    database.close()
    events.reset()
    yield
    database.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch):
    """Настоящий uvicorn на своём порту.

    Нужен там, где ASGI-транспорт в памяти не годится: потоковые ответы (SSE)
    и проверка MCP-адаптера, который обязан ходить по реальному HTTP.
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
        raise RuntimeError("ядро не поднялось")

    monkeypatch.setenv("NOTED_API", base)
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def app_factory():
    import main

    return main.create_app()
