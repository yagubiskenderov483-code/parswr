from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    telethon_session: str
    stars_per_ton: float
    poll_interval: float
    markets: tuple[str, ...]
    mrkt_token: str
    portals_auth: str
    tonnel_auth: str
    seen_path: Path
    subs_path: Path
    session_path: Path


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируй .env.example → .env")

    markets_raw = os.getenv("MARKETS", "tonnel,mrkt,portals")
    markets = tuple(m.strip().lower() for m in markets_raw.split(",") if m.strip())

    session_name = os.getenv("TELETHON_SESSION", "market_session").strip()
    return Settings(
        bot_token=token,
        api_id=int(os.getenv("API_ID", "0") or 0),
        api_hash=os.getenv("API_HASH", "").strip(),
        telethon_session=session_name,
        stars_per_ton=float(os.getenv("STARS_PER_TON", "400")),
        poll_interval=max(1.0, float(os.getenv("POLL_INTERVAL", "2"))),
        markets=markets or ("tonnel",),
        mrkt_token=os.getenv("MRKT_TOKEN", "").strip(),
        portals_auth=os.getenv("PORTALS_AUTH", "").strip(),
        tonnel_auth=os.getenv("TONNEL_AUTH", "").strip(),
        seen_path=DATA_DIR / "seen_lots.json",
        subs_path=DATA_DIR / "subscriptions.json",
        session_path=DATA_DIR / session_name,
    )
