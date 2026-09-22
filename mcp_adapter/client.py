"""HTTP-клиент к ядру.

Адаптер не трогает базу: он только переводит вызовы инструментов в запросы.
Если ядро не поднято, наружу уходит конверт api_unavailable с адресом API,
а не таймаут и не трейсбек.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_API = "http://127.0.0.1:8787"
REQUEST_TIMEOUT = 10.0
TOKEN_HEADER = "X-Noted-Token"


def api_url() -> str:
    return os.environ.get("NOTED_API", DEFAULT_API).rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("NOTED_TOKEN", "").strip()
    return {TOKEN_HEADER: token} if token else {}


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "outcome": "api_unavailable",
        "message": f"ядро noted не отвечает на {api_url()}: {detail}. Запустите `noted-api`.",
        "task": None,
    }


async def request(method: str, path: str, *, json: Any = None, params: Any = None, timeout: float | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=api_url(), headers=_headers(), timeout=timeout or REQUEST_TIMEOUT) as client:
            response = await client.request(method, path, json=json, params=params)
    except httpx.TimeoutException as exc:
        return _unavailable(f"таймаут ({exc.__class__.__name__})")
    except httpx.HTTPError as exc:
        return _unavailable(str(exc) or exc.__class__.__name__)

    try:
        return response.json()
    except ValueError:
        return {
            "ok": False,
            "outcome": "internal_error",
            "message": f"ядро вернуло не-JSON, код {response.status_code}",
            "task": None,
        }
