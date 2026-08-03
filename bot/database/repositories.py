from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.database.models import AppSettings, ExchangeRate, ParseRun, SeenLot
from bot.models import MarketName, UnifiedLot


async def get_or_create_settings(session: AsyncSession, user_id: int) -> AppSettings:
    result = await session.execute(select(AppSettings).where(AppSettings.user_id == user_id))
    row = result.scalar_one_or_none()
    if row:
        return row
    cfg = get_settings()
    row = AppSettings(
        user_id=user_id,
        min_stars=cfg.default_min_stars,
        max_stars=cfg.default_max_stars,
        poll_interval=cfg.default_poll_interval,
    )
    session.add(row)
    await session.flush()
    return row


async def update_settings(session: AsyncSession, user_id: int, **fields: Any) -> AppSettings:
    row = await get_or_create_settings(session, user_id)
    for key, value in fields.items():
        if hasattr(row, key) and value is not None:
            setattr(row, key, value)
    await session.flush()
    return row


async def is_seen(session: AsyncSession, fingerprint: str) -> bool:
    result = await session.execute(
        select(SeenLot.id).where(SeenLot.fingerprint == fingerprint).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def mark_seen(session: AsyncSession, lot: UnifiedLot) -> SeenLot:
    row = SeenLot(
        fingerprint=lot.fingerprint,
        market=lot.market.value,
        external_id=lot.external_id,
        title=lot.display_title()[:500],
        price_stars=lot.price_stars,
        original_price=lot.original_price,
        original_currency=lot.original_currency.value,
        difficulty=lot.difficulty.value,
        url=lot.url,
        found_at=lot.found_at,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_seen(session: AsyncSession, fingerprints: list[tuple[str, MarketName, str]]) -> int:
    """Mark existing lots as seen without notifying."""
    added = 0
    for fp, market, external_id in fingerprints:
        if await is_seen(session, fp):
            continue
        session.add(
            SeenLot(
                fingerprint=fp,
                market=market.value,
                external_id=external_id,
                title="",
                price_stars=0,
            )
        )
        added += 1
    await session.flush()
    return added


async def start_parse_run(session: AsyncSession, user_id: int) -> ParseRun:
    run = ParseRun(user_id=user_id, status="running")
    session.add(run)
    await session.flush()
    return run


async def stop_parse_run(session: AsyncSession, run_id: int, lots_found: int) -> None:
    result = await session.execute(select(ParseRun).where(ParseRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        return
    run.stopped_at = datetime.now(timezone.utc)
    run.lots_found = lots_found
    run.status = "stopped"
    await session.flush()


async def save_rates(session: AsyncSession, ton_usd: float, stars_usd: float) -> ExchangeRate:
    row = ExchangeRate(ton_usd=ton_usd, stars_usd=stars_usd)
    session.add(row)
    await session.flush()
    return row


async def latest_rates(session: AsyncSession) -> ExchangeRate | None:
    result = await session.execute(
        select(ExchangeRate).order_by(ExchangeRate.id.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def stats_summary(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    total = await session.scalar(select(func.count()).select_from(SeenLot).where(SeenLot.price_stars > 0))
    today_count = await session.scalar(
        select(func.count())
        .select_from(SeenLot)
        .where(SeenLot.price_stars > 0, SeenLot.found_at >= day_start)
    )

    by_market_rows = await session.execute(
        select(SeenLot.market, func.count())
        .where(SeenLot.price_stars > 0)
        .group_by(SeenLot.market)
    )
    by_diff_rows = await session.execute(
        select(SeenLot.difficulty, func.count())
        .where(SeenLot.price_stars > 0)
        .group_by(SeenLot.difficulty)
    )
    last = await session.scalar(select(func.max(SeenLot.found_at)).where(SeenLot.price_stars > 0))
    last_run = await session.scalar(select(func.max(ParseRun.started_at)))

    return {
        "total": int(total or 0),
        "today": int(today_count or 0),
        "by_market": {k: int(v) for k, v in by_market_rows.all()},
        "by_difficulty": {k: int(v) for k, v in by_diff_rows.all()},
        "last_lot_at": last,
        "last_run_at": last_run,
    }
