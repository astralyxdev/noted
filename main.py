"""The core entry point: build the FastAPI application and run uvicorn."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import settings
from api.models import database
from api.models.envelope import Outcome, body, envelope
from api.routes import live_router, tasks_router
from api.routes.mcp import build as build_mcp
from api.services import agents as agents_service
from api.services import tasks as tasks_service
from api.services.tasks import TaskError
from api.utils import events as task_events
from api.utils import run_service
from api.utils import respond
from api.utils.authorization import middleware as token_middleware

log = logging.getLogger("noted")

async def reaper() -> None:
    """The collector: expired leases and dead sessions back to the queue, ended
    retry pauses announced, the journal trimmed, dead session rows dropped.

    Without it `stale_seconds` only gives detection: a crashed agent leaves its
    task hanging forever. Here it repairs itself.
    """
    interval = settings.reap_interval_s()
    while True:
        await asyncio.sleep(interval)
        try:
            await _collect()
        except Exception:  # noqa: BLE001 - one failure must not kill the collector
            # Every job is inside this, not only the first. A transient store
            # error in any of the others used to raise out of the loop, and
            # since nothing awaits this task until shutdown the exception was
            # swallowed: leases silently stopped being reclaimed for the rest
            # of the process's life.
            log.exception("the collector stumbled")


async def _collect() -> None:
    """One round of it."""
    # A quiet session is not closed — liveness is read from `renewed_at`
    # directly, so a client silent through one long step can carry on after.
    requeued = await run_service(tasks_service.reap_expired)

    # A pause that has ended is work appearing, just like an expired lease:
    # the row was written minutes ago and became claimable in silence.
    due = await run_service(tasks_service.due_retries)
    if requeued or due:
        if requeued:
            log.info("leases expired, tasks returned to the queue: %s", requeued)
        await task_events.notify_new_task()

    trimmed = await run_service(tasks_service.trim_journal)
    if trimmed:
        log.info("journal trimmed: %s entries of closed tasks", trimmed)

    await run_service(agents_service.prune_sessions)


def ui_dir() -> Path:
    return settings.ui_dir()


def create_app() -> FastAPI:
    # One MCP server per application: the transport session manager may be run
    # exactly once in its lifetime.
    mcp = build_mcp()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            collector = asyncio.create_task(reaper())
            try:
                yield
            finally:
                collector.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await collector
                # A PostgreSQL pool holds threads of its own; letting the
                # process exit around them prints noise and leaks connections.
                database.close()

    app = FastAPI(
        title="noted",
        version="0.3.0",
        description="A task manager for agents: API, MCP and dashboard on one port",
        lifespan=lifespan,
    )
    app.middleware("http")(token_middleware)
    app.include_router(tasks_router)
    app.include_router(live_router)
    # MCP over HTTP on the same port: no separate adapter process is needed.
    app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Liveness that means something.

        Answering 200 from a handler that touches nothing said only that Python
        was running. With the database unreachable the container still reported
        healthy — for ever, and to anything waiting on `service_healthy` —
        while every real request failed. So the check reaches the store.
        """
        try:
            await run_service(database.ping)
        except Exception as exc:  # noqa: BLE001 - any failure to reach the store is unhealthy
            log.warning("health check could not reach the store: %s", exc)
            return respond(envelope(Outcome.internal_error, "the store is not answering"))
        return respond(envelope(Outcome.ok))

    @app.exception_handler(TaskError)
    async def _task_error(request: Request, exc: TaskError):
        return respond(envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's own format is overridden: an agent must get the same
        # envelope as everywhere else, not somebody else's shape of answer.
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request body"
        return respond(envelope(Outcome.validation_error, f"{where}: {first.get('msg', 'malformed request')}"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # The status code is preserved: turning a 405 into a 500 lies to the client.
        if exc.status_code == 404:
            outcome = Outcome.not_found
        elif exc.status_code == 401:
            outcome = Outcome.unauthorized
        elif exc.status_code == 403:
            outcome = Outcome.forbidden
        elif 400 <= exc.status_code < 500:
            outcome = Outcome.validation_error
        else:
            outcome = Outcome.internal_error
        return JSONResponse(
            status_code=exc.status_code,
            content=body(envelope(outcome, str(exc.detail))),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        incident = uuid.uuid4().hex[:8]
        log.exception("unhandled error [%s] %s %s", incident, request.method, request.url.path)
        return respond(envelope(Outcome.internal_error, f"internal error, see log entry {incident}"))

    # The dashboard is mounted last: it takes the root, but only the paths the
    # routers above did not claim.
    dist = ui_dir()
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    else:
        log.warning("dashboard is not built (%s missing) - API only", dist / "index.html")

    return app


app = create_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = settings.host()
    port = settings.port()
    # The application is passed as an object rather than an import string,
    # which guarantees a single process. Several workers would mean several
    # writers to SQLite — exactly the cross-process race this service exists
    # to remove. PostgreSQL would take the writers, but the long-poll and the
    # event stream are an in-process bus: a second worker would not hear the
    # first one's tasks appear. One process either way.
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
