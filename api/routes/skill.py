"""The agent skill, served at /skill.md.

One file teaches an agent how to work this queue: connecting the MCP, the
executor loop, the shape of a task, relations and decomposition. It is served
rather than shipped so an agent can fetch it from whatever address it already
reaches the core on — and that address is written into it, so the install
commands inside are right for this server, not for a default port.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["skill"])

SKILL = Path(__file__).resolve().parent.parent / "skill.md"


@cache
def _template() -> str:
    return SKILL.read_text(encoding="utf-8")


@router.api_route("/skill.md", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def skill(request: Request) -> PlainTextResponse:
    """The skill for agents working through this server's MCP."""
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(
        _template().replace("{{BASE_URL}}", base),
        media_type="text/markdown; charset=utf-8",
    )
