from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from bot.database import session_scope
from bot.database.repositories import latest_rates, save_rates

logger = logging.getLogger(__name__)

# Fallback: App Store / Google Play Star pack average ≈ $0.013–$0.015
DEFAULT_STARS_USD = 0.013
DEFAULT_TON_USD = 1.5


@dataclass(slots=True)
class Rates:
    ton_usd: float
    stars_usd: float

    @property
    def ton_to_stars(self) -> float:
        if self.stars_usd <= 0:
            return 0.0
        return self.ton_usd / self.stars_usd


class RateService:
    def __init__(self) -> None:
        self.current = Rates(ton_usd=DEFAULT_TON_USD, stars_usd=DEFAULT_STARS_USD)

    async def load_cached(self) -> Rates:
        async with session_scope() as session:
            row = await latest_rates(session)
            if row and row.ton_usd > 0 and row.stars_usd > 0:
                self.current = Rates(ton_usd=row.ton_usd, stars_usd=row.stars_usd)
        return self.current

    async def refresh(self) -> Rates:
        ton_usd = await self._fetch_ton_usd()
        stars_usd = await self._fetch_stars_usd(ton_usd)
        self.current = Rates(ton_usd=ton_usd, stars_usd=stars_usd)
        async with session_scope() as session:
            await save_rates(session, ton_usd, stars_usd)
        logger.info(
            "Rates updated: 1 TON = $%.4f | 1 ⭐ = $%.5f | 1 TON ≈ %.2f ⭐",
            ton_usd,
            stars_usd,
            self.current.ton_to_stars,
        )
        return self.current

    async def _fetch_ton_usd(self) -> float:
        urls = [
            "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd",
            "https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT",
        ]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            # CoinGecko
            try:
                async with http.get(urls[0]) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        value = float(data["the-open-network"]["usd"])
                        if value > 0:
                            return value
            except Exception as exc:  # noqa: BLE001
                logger.warning("CoinGecko TON rate failed: %s", exc)

            # Binance
            try:
                async with http.get(urls[1]) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        value = float(data["price"])
                        if value > 0:
                            return value
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance TON rate failed: %s", exc)

        logger.warning("Using fallback TON/USD = %s", DEFAULT_TON_USD)
        return self.current.ton_usd or DEFAULT_TON_USD

    async def _fetch_stars_usd(self, ton_usd: float) -> float:
        """Estimate Stars USD from public sources with robust fallbacks."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            # Fragment sometimes embeds pricing; try secondary public endpoints
            candidates = [
                "https://api.coingecko.com/api/v3/simple/price?ids=telegram&vs_currencies=usd",
            ]
            for url in candidates:
                try:
                    async with http.get(url) as resp:
                        if resp.status != 200:
                            continue
                        # Not a direct star feed — ignore content, keep fallback chain
                        await resp.text()
                except Exception:  # noqa: BLE001
                    continue

            # Derive from known Fragment star packs when possible via HTML markers
            try:
                async with http.get("https://fragment.com/stars") as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Heuristic: look for patterns like "$0.0xx" near stars
                        import re

                        matches = re.findall(r"\$0\.0\d{2,4}", html)
                        values = []
                        for m in matches:
                            try:
                                values.append(float(m.replace("$", "")))
                            except ValueError:
                                pass
                        # Prefer values near typical star price
                        plausible = [v for v in values if 0.005 <= v <= 0.05]
                        if plausible:
                            return sum(plausible) / len(plausible)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fragment stars scrape failed: %s", exc)

        # Industry-standard approximate retail price for Telegram Stars
        logger.info("Using fallback Stars/USD = %s", DEFAULT_STARS_USD)
        return DEFAULT_STARS_USD
