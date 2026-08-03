from __future__ import annotations

from bot.database import session_scope
from bot.database.repositories import stats_summary
from bot.models import MARKET_TITLES, MarketName


async def build_stats_text() -> str:
    async with session_scope() as session:
        data = await stats_summary(session)

    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"📅 Найдено сегодня: <b>{data['today']}</b>",
        f"📦 Найдено всего: <b>{data['total']}</b>",
        "",
        "<b>По маркетам:</b>",
    ]
    by_market = data["by_market"] or {}
    if not by_market:
        lines.append("— пока пусто")
    else:
        for key, count in sorted(by_market.items(), key=lambda x: -x[1]):
            try:
                title = MARKET_TITLES[MarketName(key)]
            except Exception:  # noqa: BLE001
                title = key
            lines.append(f"• {title}: {count}")

    lines += ["", "<b>По категориям:</b>"]
    by_diff = data["by_difficulty"] or {}
    if not by_diff:
        lines.append("— пока пусто")
    else:
        for key, count in sorted(by_diff.items(), key=lambda x: -x[1]):
            lines.append(f"• {key}: {count}")

    last_lot = data["last_lot_at"]
    last_run = data["last_run_at"]
    lines += [
        "",
        f"🕒 Последний лот: {last_lot or '—'}",
        f"▶️ Последний запуск: {last_run or '—'}",
    ]
    return "\n".join(lines)
