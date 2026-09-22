"""Cross-cutting helpers that belong to no single domain."""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

import anyio.to_thread
from fastapi.responses import JSONResponse

from api.models.envelope import HTTP_STATUS, Envelope, body

T = TypeVar("T")


def respond(env: Envelope) -> JSONResponse:
    """The envelope on its way out; the status code comes from one table."""
    return JSONResponse(status_code=HTTP_STATUS[env.outcome], content=body(env))


async def run_service(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Services are synchronous — both stores are — so they run in a worker
    thread: blocking the event loop would stall every waiting long-poll."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
