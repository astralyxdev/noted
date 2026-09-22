"""Точка входа ядра: сборка FastAPI и запуск uvicorn."""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.models.envelope import Outcome, body, envelope
from api.routes import live_router, tasks_router
from api.routes.mcp import build as build_mcp
from api.services.tasks import TaskError
from api.utils import respond
from api.utils.authorization import middleware as token_middleware

log = logging.getLogger("noted")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def ui_dir() -> Path:
    """Собранный дэшборд. В образе он лежит рядом, локально — в dashboard/dist."""
    raw = os.environ.get("NOTED_UI_DIR")
    return Path(raw) if raw else Path(__file__).resolve().parent / "dashboard" / "dist"


def create_app() -> FastAPI:
    # Свой MCP-сервер на каждое приложение: менеджер сессий транспорта
    # запускается ровно один раз за свою жизнь.
    mcp = build_mcp()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="noted",
        version="0.3.0",
        description="Таск-менеджер для агентов: API, MCP и дэшборд на одном порту",
        lifespan=lifespan,
    )
    app.middleware("http")(token_middleware)
    app.include_router(tasks_router)
    app.include_router(live_router)
    # MCP по HTTP на том же порту: агенту не нужен отдельный процесс-адаптер.
    app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return respond(envelope(Outcome.ok))

    @app.exception_handler(TaskError)
    async def _task_error(request: Request, exc: TaskError):
        return respond(envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Перекрываем формат FastAPI: агент должен получать тот же конверт,
        # что и в остальных случаях, а не чужую форму ответа.
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())[1:]) or "тело запроса"
        return respond(envelope(Outcome.validation_error, f"{where}: {first.get('msg', 'некорректный запрос')}"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # Код ответа сохраняем как есть: подменять 405 на 500 — врать клиенту.
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
        log.exception("необработанная ошибка [%s] %s %s", incident, request.method, request.url.path)
        return respond(envelope(Outcome.internal_error, f"внутренняя ошибка, см. лог: {incident}"))

    # Дэшборд монтируется последним: он забирает корень, но только те пути,
    # которые не разобрали роутеры выше.
    dist = ui_dir()
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    else:
        log.warning("дэшборд не собран (%s не найден) — доступен только API", dist / "index.html")

    return app


app = create_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("NOTED_HOST", DEFAULT_HOST)
    port = int(os.environ.get("NOTED_PORT", DEFAULT_PORT))
    # Приложение передаётся объектом, а не строкой импорта: это гарантирует один
    # процесс. Несколько воркеров = несколько писателей в SQLite = ровно та
    # межпроцессная гонка, ради устранения которой ядро и вынесено в сервис.
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
