from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bot.categories import category_for_stars
from bot.markets import MarketClient
from bot.models import Lot
from bot.storage import SeenLotsStore

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[Lot, str], Awaitable[None]]


class LotMonitor:
    def __init__(
        self,
        clients: list[MarketClient],
        seen_store: SeenLotsStore,
        on_new_lot: NotifyCallback,
        poll_interval: float = 2.0,
    ):
        self.clients = clients
        self.seen_store = seen_store
        self.on_new_lot = on_new_lot
        self.poll_interval = poll_interval
        self._seen: set[str] = set()
        self._running = False
        self.last_fetch_count = 0
        self.last_error: str | None = None
        self.new_lots_total = 0
        self.per_market: dict[str, int] = {}

    async def run(self) -> None:
        self._running = True
        self._seen = self.seen_store.load_ids()
        primed = bool(self._seen)

        while self._running:
            try:
                batches = await asyncio.gather(
                    *[c.fetch_newest(30) for c in self.clients],
                    return_exceptions=True,
                )
                lots: list[Lot] = []
                errors: list[str] = []
                for client, batch in zip(self.clients, batches):
                    if isinstance(batch, Exception):
                        errors.append(f"{client.name}: {batch}")
                        logger.warning("Market %s failed: %s", client.name, batch)
                        continue
                    self.per_market[client.name] = len(batch)
                    lots.extend(batch)

                self.last_fetch_count = len(lots)
                self.last_error = "; ".join(errors) if errors else None
                current_ids = {lot.unique_id for lot in lots}

                if not primed:
                    self._seen = current_ids
                    self.seen_store.save_ids(self._seen)
                    primed = True
                    logger.info("Seeded %s lots across markets", len(self._seen))
                else:
                    fresh = [lot for lot in lots if lot.unique_id not in self._seen]
                    fresh.sort(key=lambda x: x.stars)

                    for lot in fresh:
                        cat = category_for_stars(lot.stars)
                        if cat is None:
                            continue
                        try:
                            await self.on_new_lot(lot, cat.key)
                            self.new_lots_total += 1
                        except Exception:
                            logger.exception("Notify failed for %s", lot.unique_id)

                    if current_ids:
                        self._seen |= current_ids
                        self.seen_store.save_ids(self._seen)

                    if fresh:
                        logger.info("New lots detected: %s", len(fresh))

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Monitor loop error: %s", exc)

            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
