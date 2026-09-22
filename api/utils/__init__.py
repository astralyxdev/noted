"""Сквозные мелочи, не привязанные к домену."""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

import anyio.to_thread
from fastapi.responses import JSONResponse

from api.models.envelope import HTTP_STATUS, Envelope, body

T = TypeVar("T")


def respond(env: Envelope) -> JSONResponse:
    """Конверт наружу: HTTP-код берётся из единственной таблицы соответствий."""
    return JSONResponse(status_code=HTTP_STATUS[env.outcome], content=body(env))


async def run_service(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Сервисы синхронные (sqlite), поэтому зовём их в отдельном потоке —
    иначе блокируется event loop и вместе с ним все ждущие long-poll."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
