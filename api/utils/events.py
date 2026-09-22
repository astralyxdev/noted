"""Waking up whoever is waiting: agents on claim, browsers on the dashboard.

The core is a single process, so two asyncio.Conditions are enough and no
broker is needed:

* `available` — work appeared that somebody may take. Wakes the claim long-poll.
* `changed`   — the state moved in any way at all. Wakes the dashboard stream.

They are split on purpose: claiming a task changes what the dashboard shows,
but waking agents that are waiting for work would wake them for nothing.
"""

from __future__ import annotations

import asyncio

_available: asyncio.Condition | None = None
_changed: asyncio.Condition | None = None


def _condition(which: str) -> asyncio.Condition:
    """Created lazily: the object has to be born inside a running loop."""
    global _available, _changed
    if which == "available":
        if _available is None:
            _available = asyncio.Condition()
        return _available
    if _changed is None:
        _changed = asyncio.Condition()
    return _changed


def reset() -> None:
    global _available, _changed
    _available = _changed = None


async def _notify(cond: asyncio.Condition) -> None:
    async with cond:
        cond.notify_all()


async def notify_new_task() -> None:
    """Work appeared: wake both the agents and the dashboard."""
    await _notify(_condition("available"))
    await _notify(_condition("changed"))


async def notify_change() -> None:
    """The state moved but no new work appeared: dashboard only."""
    await _notify(_condition("changed"))


async def _wait(cond: asyncio.Condition, timeout: float) -> bool:
    if timeout <= 0:
        return False
    try:
        async with cond:
            await asyncio.wait_for(cond.wait(), timeout)
        return True
    except (asyncio.TimeoutError, TimeoutError):
        return False


async def wait_for_task(timeout: float) -> bool:
    """True when woken, False on timeout. Either way the caller tries to claim
    again: being woken does not mean the task went to this waiter."""
    return await _wait(_condition("available"), timeout)


async def wait_for_change(timeout: float) -> bool:
    return await _wait(_condition("changed"), timeout)
