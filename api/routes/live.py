"""Поток событий для дэшборда.

Обмен односторонний (сервер → браузер), поэтому SSE, а не вебсокет:
переподключение встроено в браузер, транспорт обычный HTTP, зависимостей ноль.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from api.utils import events

router = APIRouter(include_in_schema=False)

KEEPALIVE_S = 20.0


@router.get("/events")
async def live_events():
    async def stream():
        yield "retry: 3000\n\n"
        while True:
            if await events.wait_for_change(KEEPALIVE_S):
                yield "event: tasks\ndata: changed\n\n"
            else:
                # Комментарий раз в 20 секунд: прокси не должны рвать тишину.
                yield ": keepalive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
