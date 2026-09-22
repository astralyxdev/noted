"""The HTTP client to the core.

The adapter never touches the database: it only turns tool calls into
requests. When the core is down, what comes back is an api_unavailable
envelope carrying the API address, not a timeout and not a traceback.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx

from api import settings

REQUEST_TIMEOUT = 10.0
TOKEN_HEADER = "X-Noted-Token"
SESSION_HEADER = "X-Noted-Session"
#: Says this transport renews the session by itself, so a silent session means
#: a dead process rather than a busy one.
TRANSPORT_HEADER = "X-Noted-Transport"

#: This process's session. The adapter lives exactly as long as its client, so
#: its life is the agent's proof of life — the model never thinks about it.
SESSION_ID = secrets.token_urlsafe(18)


def api_url() -> str:
    return settings.api_url()


def _headers() -> dict[str, str]:
    token = settings.agent_key()
    headers = {SESSION_HEADER: SESSION_ID, TRANSPORT_HEADER: "stdio"}
    if token:
        headers[TOKEN_HEADER] = token
    return headers


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "outcome": "api_unavailable",
        "message": f"the noted core is not answering at {api_url()}: {detail}. Start `noted-api`.",
        "task": None,
    }


async def request(method: str, path: str, *, json: Any = None, params: Any = None, timeout: float | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=api_url(), headers=_headers(), timeout=timeout or REQUEST_TIMEOUT) as client:
            response = await client.request(method, path, json=json, params=params)
    except httpx.TimeoutException as exc:
        return _unavailable(f"timed out ({exc.__class__.__name__})")
    except httpx.HTTPError as exc:
        return _unavailable(str(exc) or exc.__class__.__name__)

    try:
        return response.json()
    except ValueError:
        return {
            "ok": False,
            "outcome": "internal_error",
            "message": f"the core returned non-JSON, status {response.status_code}",
            "task": None,
        }
