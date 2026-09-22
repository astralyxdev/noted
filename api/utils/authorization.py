"""Who is calling: an admin, an agent, or nobody.

Until the first key is issued the core runs as an open queue, so an upgrade
breaks no local install. Issuing a key turns identity on: `assignee_id` stops
being a parameter and becomes a conclusion drawn from the key, and calling
yourself by somebody else's name is no longer possible.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from api import settings
from api.models.agent import Agent
from api.models.envelope import HTTP_STATUS, Outcome, body, envelope
from api.services import agents

HEADER = "X-Noted-Token"
SESSION_HEADER = "X-Noted-Session"
#: The MCP transport sends its own client session id, and that is the agent session.
MCP_SESSION_HEADER = "Mcp-Session-Id"

GUARDED = ("/api", "/mcp")
EXEMPT = ("/healthz", "/api/login")

COOKIE = "noted_admin"
COOKIE_TTL_S = 7 * 24 * 3600

#: Passes handed to browsers. Kept in memory on purpose: a restart logs people
#: out, but there is nowhere to steal a pass from and nothing to revoke by hand.
_browser_passes: dict[str, float] = {}


@dataclass(frozen=True)
class Principal:
    """Who performs the call. `agent=None` with `is_admin=False` is the open queue."""

    agent: Agent | None = None
    is_admin: bool = False
    session_id: str | None = None

    @property
    def name(self) -> str | None:
        return self.agent.id if self.agent else None

    @property
    def projects(self) -> list[str] | None:
        return self.agent.projects if self.agent else None


ANONYMOUS = Principal()


def admin_token() -> str | None:
    return settings.admin_token()


def from_headers(headers: Mapping[str, str] | None, transport: str = "http") -> Principal | None:
    """Headers to principal. None means refusal.

    One function for both transports: the middleware calls it for an HTTP
    request, an MCP tool calls it for the headers of its own call, and the
    rules cannot drift apart.
    """
    lookup = {k.lower(): v for k, v in (headers or {}).items()}
    presented = (lookup.get(HEADER.lower()) or "").strip()
    expected = admin_token()

    session_id = (lookup.get(SESSION_HEADER.lower()) or lookup.get(MCP_SESSION_HEADER.lower()) or "").strip()

    if expected and presented and hmac.compare_digest(presented.encode(), expected.encode()):
        return Principal(is_admin=True, session_id=session_id or None)

    agent = agents.resolve(presented) if presented else None
    if agent is not None:
        if session_id:
            # A live transport means a live agent. The model never thinks about it.
            agents.ensure_session(session_id, agent.id, transport)
        return Principal(agent=agent, session_id=session_id or None)

    if expected or agents.identity_required():
        return None

    return ANONYMOUS


def open_browser_pass() -> str:
    """A pass for the dashboard: a browser sends no headers and will not add any."""
    import secrets
    import time

    pass_id = secrets.token_urlsafe(24)
    _browser_passes[pass_id] = time.time() + COOKIE_TTL_S
    return pass_id


def close_browser_pass(pass_id: str | None) -> None:
    if pass_id:
        _browser_passes.pop(pass_id, None)


def _browser_admin(request: Request) -> bool:
    import time

    pass_id = request.cookies.get(COOKIE)
    if not pass_id:
        return False
    expires = _browser_passes.get(pass_id)
    if expires is None:
        return False
    if expires < time.time():
        _browser_passes.pop(pass_id, None)
        return False
    return True


def resolve(request: Request) -> Principal | JSONResponse:
    transport = "http-mcp" if request.url.path.startswith("/mcp") else "http"
    who = from_headers(request.headers, transport)
    if who is not None:
        return who
    # The dashboard cannot send the header, so it has its own door and cookie.
    if _browser_admin(request):
        return Principal(is_admin=True)
    return _denied(f"a valid key is required in the {HEADER} header")


def _denied(message: str) -> JSONResponse:
    env = envelope(Outcome.unauthorized, message)
    return JSONResponse(status_code=HTTP_STATUS[Outcome.unauthorized], content=body(env))


async def middleware(request: Request, call_next):
    if not any(request.url.path.startswith(prefix) for prefix in GUARDED) or request.url.path in EXEMPT:
        return await call_next(request)

    outcome = resolve(request)
    if isinstance(outcome, JSONResponse):
        return outcome

    request.state.principal = outcome
    return await call_next(request)


def of(request: Request) -> Principal:
    return getattr(request.state, "principal", ANONYMOUS)
