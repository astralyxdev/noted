"""Пробуждение ждущих: агентов на claim и браузеров на дэшборде.

Ядро — один процесс, поэтому хватает двух asyncio.Condition без брокера:

* `available` — появилась задача, которую можно забрать. Будит long-poll в claim.
* `changed`   — состояние изменилось вообще как-нибудь. Будит SSE-поток дэшборда.

Разделены намеренно: захват задачи меняет картину на дэшборде, но будить ради
этого агентов, ждущих работу, незачем — они проснутся впустую.
"""

from __future__ import annotations

import asyncio

_available: asyncio.Condition | None = None
_changed: asyncio.Condition | None = None


def _condition(which: str) -> asyncio.Condition:
    """Создаются лениво: объект должен родиться внутри работающего loop."""
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
    """Появилась работа: будим и агентов, и дэшборд."""
    await _notify(_condition("available"))
    await _notify(_condition("changed"))


async def notify_change() -> None:
    """Состояние изменилось, но новой работы не появилось: только дэшборд."""
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
    """True — разбудили, False — вышел таймаут. В обоих случаях вызывающий
    пробует захват заново: пробуждение не значит, что задача досталась ему."""
    return await _wait(_condition("available"), timeout)


async def wait_for_change(timeout: float) -> bool:
    return await _wait(_condition("changed"), timeout)
