"""Проверка NOTED_TOKEN.

Токен спрашиваем на /api и /mcp: браузер заголовок не пришлёт, а дэшборд и так
живёт за localhost. Не задан NOTED_TOKEN — проверки нет вовсе.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from api.models.envelope import HTTP_STATUS, Outcome, body, envelope

HEADER = "X-Noted-Token"
GUARDED = ("/api", "/mcp")
EXEMPT = ("/healthz",)


def token() -> str | None:
    value = os.environ.get("NOTED_TOKEN", "").strip()
    return value or None


async def middleware(request: Request, call_next):
    expected = token()
    guarded = any(request.url.path.startswith(prefix) for prefix in GUARDED)
    if expected is None or not guarded or request.url.path in EXEMPT:
        return await call_next(request)

    given = request.headers.get(HEADER, "")
    if not hmac.compare_digest(given, expected):
        env = envelope(Outcome.unauthorized, f"нужен заголовок {HEADER}")
        return JSONResponse(status_code=HTTP_STATUS[Outcome.unauthorized], content=body(env))
    return await call_next(request)
