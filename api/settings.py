"""Every environment-backed setting in one place.

Values are read on each call rather than cached at import: tests override them
with monkeypatch, and the container overrides them with `docker run -e`. A
`.env` file next to the project is loaded once at startup, so a local install
needs no exported variables at all.

This module deliberately imports nothing from the rest of the package — every
layer may depend on it, and it may depend on none of them.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

#: `.env` never overrides a variable that is already exported: an explicit
#: `docker run -e` or a shell export must win over a file left in the tree.
load_dotenv(ROOT / ".env", override=False)


def _text(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _whole(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# ── where things live ────────────────────────────────────────────────────

def database_path() -> Path:
    raw = os.environ.get("NOTED_DB")
    return Path(raw).expanduser() if raw else Path.home() / ".noted" / "tasks.db"


def ui_dir() -> Path:
    raw = os.environ.get("NOTED_UI_DIR")
    return Path(raw) if raw else ROOT / "dashboard" / "dist"


# ── network ──────────────────────────────────────────────────────────────

def host() -> str:
    return _text("NOTED_HOST", "127.0.0.1")


def port() -> int:
    return _whole("NOTED_PORT", 8787, minimum=1)


def api_url() -> str:
    """Where the stdio adapter looks for the core."""
    return _text("NOTED_API", "http://127.0.0.1:8787").rstrip("/")


def admin_token() -> str | None:
    return (os.environ.get("NOTED_TOKEN") or "").strip() or None


def agent_key() -> str | None:
    """Agent key for the stdio adapter; falls back to the shared token."""
    return (os.environ.get("NOTED_KEY") or os.environ.get("NOTED_TOKEN") or "").strip() or None


# ── liveness and recovery ────────────────────────────────────────────────

def lease_s() -> float:
    """Task lease for claims made outside a session."""
    return _number("NOTED_LEASE_S", 300.0)


def session_ttl_s() -> float:
    """How long a session survives without renewal."""
    return _number("NOTED_SESSION_TTL_S", 90.0)


def reap_interval_s() -> float:
    return _number("NOTED_REAP_INTERVAL_S", 15.0)


# ── retries ──────────────────────────────────────────────────────────────

def retry_base_s() -> float:
    """First pause before a retry; doubles from there."""
    return _number("NOTED_RETRY_BASE_S", 5.0)


def retry_cap_s() -> float:
    return _number("NOTED_RETRY_CAP_S", 300.0)


# ── guard rails ──────────────────────────────────────────────────────────

def create_limit() -> int:
    """Tasks one author may queue per window; 0 disables the guard."""
    return _whole("NOTED_CREATE_LIMIT", 300)


def create_window_s() -> float:
    return _number("NOTED_CREATE_WINDOW_S", 60.0)


def journal_keep_days() -> float:
    """Days of journal kept for closed tasks; 0 keeps everything."""
    return _number("NOTED_JOURNAL_KEEP_DAYS", 30.0)
