"""The event stream — a public contract for integrations.

The exchange runs one way (server to browser or script), so SSE rather than a
websocket: reconnection is built into the client, the transport is plain HTTP,
and there are no dependencies.

Event format:

    id: <journal entry number>
    event: task
    data: {"id":12,"task_id":7,"at":"…","event":"claimed",
            "actor":"agent-1","from_status":"pending","to_status":"in_progress",
            "detail":{…}}

`id` is the journal entry number and the cursor at once. A reconnecting client
sends `Last-Event-ID` (a browser EventSource does it by itself) or
`?after=<number>` and receives everything it missed, not just what is new.
That is what makes integrations possible on top of the stream, rather than
only lighting up the dashboard.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.services import tasks as tasks_service
from api.utils import events, run_service
from api.utils.authorization import of as principal_of

router = APIRouter(include_in_schema=False)

KEEPALIVE_S = 20.0
BATCH = 200


def _frame(event) -> str:
    payload = {
        "id": event.id,
        "task_id": event.task_id,
        "at": event.at,
        "event": event.event,
        "actor": event.actor,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value if event.to_status else None,
        "detail": event.detail,
    }
    return f"id: {event.id}\nevent: task\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _cursor(request: Request, after: int | None) -> int:
    """Where to resume. With no cursor we send only what is new, not history."""
    resumed = request.headers.get("Last-Event-ID") or (str(after) if after is not None else None)
    if resumed:
        try:
            return max(0, int(resumed))
        except ValueError:
            pass
    return await run_service(tasks_service.last_event_id)


@router.get("/events")
async def live_events(request: Request, after: int | None = None):
    cursor = await _cursor(request, after)
    # The journal names projects, actors and transitions, and `?after=0`
    # replays all of it. Without this a key refused on /api/tasks could read
    # every project's history here instead.
    allowed = principal_of(request).projects

    async def stream():
        nonlocal cursor
        yield "retry: 3000\n\n"
        while True:
            batch = await run_service(tasks_service.events_after, cursor, BATCH, allowed)
            if batch:
                for event in batch:
                    yield _frame(event)
                cursor = batch[-1].id
                # A full batch means we are behind; keep reading without sleeping.
                if len(batch) == BATCH:
                    continue
            elif not await events.wait_for_change(KEEPALIVE_S):
                # A comment every 20 seconds so proxies do not cut the silence.
                yield ": keepalive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
