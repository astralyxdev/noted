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
    """Returns tasks whose lease nobody renewed back to the queue.

    Without this `stale_seconds` only gives detection: a crashed agent leaves
    its task hanging forever. Here it repairs itself.
    """
    interval = settings.reap_interval_s()
    while True:
        await asyncio.sleep(interval)
        try:
            # A quiet session is not closed — the collector reads liveness from
            # `renewed_at` directly, so a client that went silent for one long
            # step can carry on afterwards.
            requeued = await run_service(tasks_service.reap_expired)
        except Exception:  # noqa: BLE001 - one failure must not kill the collector
            log.exception("the lease collector stumbled")
            continue
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
        elif exc.status_code in (401, 403):
            outcome = Outcome.unauthorized
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
    # to remove.
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
