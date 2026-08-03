from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


async def with_retries(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 2,
    delay: float = 0.25,
    label: str = "operation",
) -> T:
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("%s failed (%s/%s): %s", label, i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(delay * i)
    assert last_exc is not None
    raise last_exc
