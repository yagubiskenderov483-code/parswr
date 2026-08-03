from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from bot.models import MarketName, RawLot
from bot.utils import with_retries

logger = logging.getLogger(__name__)


class BaseMarketParser(ABC):
    """Common interface for all gift marketplace parsers."""

    name: MarketName
    title: str

    def __init__(self) -> None:
        self.enabled = True
        self.last_error: str | None = None
        self.last_count = 0

    @abstractmethod
    async def fetch_latest(self, limit: int = 30) -> list[RawLot]:
        """Return newest listed lots (best-effort chronological order)."""

    async def safe_fetch(self, limit: int = 30) -> list[RawLot]:
        if not self.enabled:
            return []

        async def _run() -> list[RawLot]:
            return await self.fetch_latest(limit=limit)

        try:
            lots = await with_retries(_run, attempts=3, delay=0.8, label=self.title)
            self.last_error = None
            self.last_count = len(lots)
            return lots
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.error("API error [%s]: %s", self.title, exc)
            return []
