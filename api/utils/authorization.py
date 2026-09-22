"""Who is calling: an admin, an agent, or nobody.

Until the first key is issued the core runs as an open queue, so an upgrade
breaks no local install. Issuing a key turns identity on: `assignee_id` stops
being a parameter and becomes a conclusion drawn from the key, and calling
yourself by somebody else's name is no longer possible.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from api import settings
from api.models.agent import SELF_RENEWING, Agent
from api.models.envelope import HTTP_STATUS, Outcome, body, envelope
from api.services import agents

HEADER = "X-Noted-Token"
SESSION_HEADER = "X-Noted-Session"
TRANSPORT_HEADER = "X-Noted-Transport"

#: The MCP transport sends its own client session id, and that is the agent session.
MCP_SESSION_HEADER = "Mcp-Session-Id"

#: /events carries the whole journal — actors, projects, payload details —
#: so it sits behind the same door as the API.
#: `/docs`, `/redoc` and `/openapi.json` describe every route and body, so they
#: sit behind the same door. With no token and no keys the queue is open and
#: they are open with it.
GUARDED = ("/api", "/mcp", "/events", "/docs", "/redoc", "/openapi.json")
EXEMPT = ("/healthz", "/api/login")

COOKIE = "noted_admin"
COOKIE_TTL_S = 7 * 24 * 3600

#: Passes handed to browsers. Kept in memory on purpose: a restart logs people
#: out, but there is nowhere to steal a pass from and nothing to revoke by hand.
_browser_passes: dict[str, float] = {}

#: Failed dashboard logins per client address. `NOTED_TOKEN` is a secret a
#: person chose, and the door was answering wrong guesses as fast as they
#: arrived. Kept in memory like the passes: a restart forgets it, which is the
#: right trade for a queue that is meant to run on a loopback port.
_login_failures: dict[str, list[float]] = {}
LOGIN_TRIES = 8
LOGIN_WINDOW_S = 60.0


def login_allowed(client: str) -> bool:
    """Whether this address may still try. Also prunes what has aged out."""
    fresh = [at for at in _login_failures.get(client, []) if at > time.time() - LOGIN_WINDOW_S]
    if fresh:
        _login_failures[client] = fresh
    else:
        _login_failures.pop(client, None)
    return len(fresh) < LOGIN_TRIES


def login_failed(client: str) -> None:
    _login_failures.setdefault(client, []).append(time.time())


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
        """None means every project. An open queue has no scope either."""
        return self.agent.projects if self.agent else None

    def may_touch(self, project: str | None) -> bool:
        return self.agent.may_touch(project) if self.agent else True


ANONYMOUS = Principal()


def admin_token() -> str | None:
    return settings.admin_token()


def from_headers(
    headers: Mapping[str, str] | None,
    transport: str = "http",
    renew: bool = True,
) -> Principal | None:
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
        if session_id and renew:
            # A live transport means a live agent. The model never thinks about it.
            session = agents.ensure_session(session_id, agent.id, transport)
            if session is None:
                # Somebody else's session id, or one that was already closed.
                return None
        return Principal(agent=agent, session_id=session_id or None, is_admin=agent.is_admin)

    if expected or agents.identity_required():
        return None

    return ANONYMOUS


def open_browser_pass() -> str:
    """A pass for the dashboard: a browser sends no headers and will not add any."""
    pass_id = secrets.token_urlsafe(24)
    _browser_passes[pass_id] = time.time() + COOKIE_TTL_S
    return pass_id


def close_browser_pass(pass_id: str | None) -> None:
    if pass_id:
        _browser_passes.pop(pass_id, None)


def _browser_admin(pass_id: str | None) -> bool:
    """Is this pass still good? Takes the pass rather than the request: an MCP
    tool has only headers to work from, and both doors must agree."""
    if not pass_id:
        return False
    expires = _browser_passes.get(pass_id)
    if expires is None:
        return False
    if expires < time.time():
        _browser_passes.pop(pass_id, None)
        return False
    return True


def transport_of(headers: Mapping[str, str] | None, path: str) -> str:
    """Which kind of client this call arrived from.

    Only a client that renews the session by itself may be trusted to prove
    death by silence. A plain MCP or API client sends nothing during a long
    step, so going quiet says nothing about whether it is alive, and the lease
    is what governs its hold. A supervisor that renews in the background can
    say so with `X-Noted-Transport: self-renewing` and get its tasks released
    the moment it dies.
    """
    declared = {k.lower(): v for k, v in (headers or {}).items()}.get(TRANSPORT_HEADER.lower())
    if declared and declared.strip() == SELF_RENEWING:
        return SELF_RENEWING
    return "http-mcp" if path.startswith("/mcp") else "http"


def principal_from(
    headers: Mapping[str, str] | None,
    transport: str = "http",
    renew: bool = True,
) -> Principal | None:
    """Headers to principal, counting the dashboard's cookie as well.

    The middleware has two doors — a key in a header, and a browser pass in a
    cookie — and anything that works out a principal for itself has to know
    about both. `from_headers` alone silently answers ANONYMOUS to a caller the
    middleware has already let in as an administrator, and then the two
    disagree about who is calling.
    """
    who = from_headers(headers, transport, renew)
    if who is not None:
        return who
    lookup = {k.lower(): v for k, v in (headers or {}).items()}
    for crumb in (lookup.get("cookie") or "").split(";"):
        name, _, value = crumb.strip().partition("=")
        if name == COOKIE and _browser_admin(value):
            return Principal(is_admin=True)
    return None


def resolve(request: Request) -> Principal | JSONResponse:
    transport = transport_of(request.headers, request.url.path)
    who = from_headers(request.headers, transport)
    if who is not None:
        return who
    # The dashboard cannot send the header, so it has its own door and cookie.
    if _browser_admin(request.cookies.get(COOKIE)):
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
