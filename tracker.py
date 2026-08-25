"""
Гифт-трекер внутреннего маркета Telegram.

Ловит только что выставленные на перепродажу NFT-подарки (за Stars),
фильтрует по цене MIN_STARS..MAX_STARS и постит карточки в канал.

Запуск:  python3 tracker.py
Настройки берутся из .env (см. .env.example) или переменных окружения.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.utils import get_peer_id

import market as market_mod
from market import (
    Lot,
    TelegramMarket,
    format_account_level,
    is_free_dm_lot,
    is_russian_lot,
)

logger = logging.getLogger("tracker")

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_BOT_TOKEN = "8807847926:AAEjVPEqkFcX76QXsI6ftnh33OrOQ3knywM"
DEFAULT_TARGET_CHANNEL = "https://t.me/+i-rzZn2WNhMwZmQ1"


def data_dir() -> Path:
    """Bothost хранит данные в /app/data; локально — рядом со скриптом."""
    bothost = Path("/app/data")
    if bothost.is_dir():
        return bothost
    return BASE_DIR


def _load_dotenv() -> None:
    """Мини-загрузчик .env: переменные окружения имеют приоритет."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


@dataclass(slots=True)
class Config:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str
    target_channel: str
    min_stars: float = 500.0
    max_stars: float = 900.0
    poll_interval: float = 2.0
    page_limit: int = 12
    parallel: int = 8
    gap: float = 0.05
    timeout: float = 6.0
    ton_rate: float = 0.0102  # TON за 1 Star (для строки "X Stars / Y TON")
    tz_offset: float = 3.0  # часовой пояс для времени в карточке (МСК = 3)
    session_file: str = ""
    state_file: str = ""
    post_on_first_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return default

        api_id = int(os.environ.get("API_ID", "0") or 0)
        api_hash = os.environ.get("API_HASH", "").strip()
        if not api_id or not api_hash:
            try:
                import credentials as creds

                api_id = api_id or int(getattr(creds, "API_ID", 0) or 0)
                api_hash = api_hash or str(getattr(creds, "API_HASH", "") or "")
            except ImportError:
                pass
        if not api_id or not api_hash:
            raise SystemExit("API_ID/API_HASH не заданы — заполни .env или credentials.py")

        dd = data_dir()
        session_file = os.environ.get(
            "SESSION_FILE", str(dd / "tracker_session.txt")
        ).strip()
        state_file = os.environ.get(
            "STATE_FILE", str(dd / "tracker_state.json")
        ).strip()
        bot_token = os.environ.get("BOT_TOKEN", "").strip() or DEFAULT_BOT_TOKEN
        target = (
            os.environ.get("TARGET_CHANNEL", "").strip() or DEFAULT_TARGET_CHANNEL
        )
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_string=os.environ.get("SESSION_STRING", "").strip(),
            bot_token=bot_token,
            target_channel=target,
            min_stars=_f("MIN_STARS", 500),
            max_stars=_f("MAX_STARS", 900),
            poll_interval=_f("POLL_INTERVAL", 2.0),
            page_limit=int(_f("PAGE_LIMIT", 12)),
            parallel=int(_f("PARALLEL", 8)),
            gap=_f("REQUEST_GAP", 0.05),
            timeout=_f("REQUEST_TIMEOUT", 6.0),
            ton_rate=_f("TON_RATE", 0.0102),
            tz_offset=_f("TZ_OFFSET", 3.0),
            session_file=session_file,
            state_file=state_file,
            post_on_first_run=os.environ.get("POST_ON_FIRST_RUN", "0") == "1",
        )


# ---------------------------------------------------------------- state

SEEN_TTL = 7 * 24 * 3600  # помним лот неделю — дальше номер уже не «новый»
SELLER_TTL = 90 * 24 * 3600  # одного продавца не постим повторно 90 дней


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("seen", {})
            data.setdefault("seen_sellers", {})
            data.setdefault("channel_id", None)
            return data
    except (OSError, ValueError):
        pass
    return {"seen": {}, "seen_sellers": {}, "channel_id": None}


def save_state(path: Path, state: dict) -> None:
    now = time.time()
    seen = state.get("seen", {})
    if len(seen) > 200_000:
        state["seen"] = {
            k: v for k, v in seen.items() if now - float(v) < SEEN_TTL
        }
    sellers = state.get("seen_sellers", {})
    if len(sellers) > 100_000:
        state["seen_sellers"] = {
            k: v for k, v in sellers.items() if now - float(v) < SELLER_TTL
        }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- format

_esc = html.escape


def lot_slug(lot: Lot) -> str:
    if lot.slug:
        return lot.slug
    if lot.number is not None:
        clean = "".join(ch for ch in lot.title if ch.isalnum())
        return f"{clean}-{lot.number}"
    return lot.title


def format_lot(lot: Lot, cfg: Config, ts: float | None = None) -> str:
    stars = int(lot.stars) if float(lot.stars).is_integer() else lot.stars
    ton = lot.stars * cfg.ton_rate
    tz = timezone(timedelta(hours=cfg.tz_offset))
    when = datetime.fromtimestamp(ts or time.time(), tz=tz).strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    if lot.seller:
        seller = f"@{lot.seller}"
        if lot.seller_id:
            seller += f" (<code>{lot.seller_id}</code>)"
    elif lot.seller_id:
        seller = f"<code>{lot.seller_id}</code>"
    else:
        seller = "скрыт"

    if lot.free_dm is True:
        dm = "бесплатно"
    elif lot.free_dm is False:
        dm = f"платно ({lot.paid_dm_stars} ⭐)" if lot.paid_dm_stars else "платно"
    else:
        dm = "—"

    if lot.is_premium is True:
        status = "Premium"
    elif lot.is_premium is False:
        status = "без Premium"
    else:
        status = "—"

    return "\n".join(
        [
            "🎉 <b>НОВЫЙ ЛИСТИНГ</b>",
            "",
            f"🎁 Гифт: <b>{_esc(lot.title)}</b>",
            f"💲 Цена: <b>{stars} Stars / {ton:.2f} TON</b>",
            f"🏷 Модель: <b>{_esc(lot.model) or '—'}</b>",
            f"👤 Продавец: {seller}",
            f"📶 Level: {format_account_level(lot)}",
            f"📢 Сообщения: {dm}",
            f"🕺 Статус: {status}",
            f'🔗 <a href="{lot.nft_url}">{_esc(lot_slug(lot))}</a>',
            f"🕒 {when}",
        ]
    )


# ---------------------------------------------------------------- sending


class Sender:
    """Шлёт карточки: через бота (с кнопками) или от юзер-сессии."""

    def __init__(self, cfg: Config, client: TelegramClient) -> None:
        self.cfg = cfg
        self.client = client
        self.chat_id: int | None = None
        self._bot = None
        if cfg.bot_token:
            from aiogram import Bot

            self._bot = Bot(token=cfg.bot_token)

    def _keyboard(self, lot: Lot):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [[InlineKeyboardButton(text="✓ Занять", url=lot.nft_url)]]
        second = [InlineKeyboardButton(text="🎁 Открыть лот", url=lot.nft_url)]
        if lot.seller:
            second.append(
                InlineKeyboardButton(
                    text="👤 Продавец", url=f"https://t.me/{lot.seller}"
                )
            )
        rows.append(second)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def send(self, lot: Lot) -> None:
        text = format_lot(lot, self.cfg)
        if self._bot is not None:
            from aiogram.types import LinkPreviewOptions

            await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=self._keyboard(lot),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await self.client.send_message(
                self.chat_id, text, parse_mode="html", link_preview=False
            )

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()


async def resolve_channel(client: TelegramClient, raw: str) -> int:
    """@username / -100id / инвайт-ссылка t.me/+hash -> numeric chat id."""
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    m = re.search(r"(?:t\.me/\+|t\.me/joinchat/|^\+)([A-Za-z0-9_-]+)", raw)
    if m:
        invite = m.group(1)
        try:
            updates = await client(ImportChatInviteRequest(invite))
            chat = updates.chats[0]
        except UserAlreadyParticipantError:
            info = await client(CheckChatInviteRequest(invite))
            chat = getattr(info, "chat", None)
            if chat is None:
                raise SystemExit(
                    "Не удалось получить канал по инвайт-ссылке — "
                    "вступи в канал этим аккаунтом и повтори"
                )
        return get_peer_id(chat)
    entity = await client.get_entity(raw)
    return get_peer_id(entity)


# ---------------------------------------------------------------- tracker


async def poll_once(
    m: TelegramMarket,
    gift_ids: list[int],
    seen: dict[str, float],
    cfg: Config,
    *,
    baseline: bool,
) -> list[Lot]:
    """Один проход по всем коллекциям: первая страница = самые свежие лоты."""
    stats = {"ok": 0, "errors": 0, "floods": 0}
    sem = asyncio.Semaphore(cfg.parallel)

    async def one(gid: int) -> list[Lot]:
        async with sem:
            result = await m._request(
                gid, cfg.page_limit, True, stats, cfg.gap, cfg.timeout
            )
            if result is None:
                return []
            return market_mod._parse_result(result)

    chunks = await asyncio.gather(
        *(one(g) for g in gift_ids), return_exceptions=True
    )
    now = time.time()
    fresh: list[Lot] = []
    for lots in chunks:
        if isinstance(lots, BaseException):
            continue
        for lot in lots:
            if lot.id in seen:
                continue
            seen[lot.id] = now
            if baseline:
                continue
            if cfg.min_stars <= lot.stars <= cfg.max_stars:
                fresh.append(lot)
    if stats["floods"]:
        logger.warning("FloodWait x%s за проход — снижаю темп", stats["floods"])
    return fresh


async def enrich(m: TelegramMarket, lots: list[Lot]) -> None:
    """Дотянуть username, lvl, статус ЛС — для каждого нового лота."""
    for lot in lots:
        if not lot.seller or not lot.seller_id:
            try:
                await m.resolve_owner(lot, timeout=2.5)
            except Exception:  # noqa: BLE001
                pass
    need_lvl = [lot for lot in lots if lot.seller_id is not None]
    if need_lvl:
        try:
            await m.enrich_profiles(need_lvl, timeout=3.0, parallel=4)
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrich_profiles: %s", exc)
    try:
        await m.check_free_dm(lots, timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_free_dm: %s", exc)


def filter_for_post(
    lots: list[Lot], seen_sellers: dict[str, float], *, now: float
) -> list[Lot]:
    """Только RU + бесплатные ЛС + один раз на продавца."""
    out: list[Lot] = []
    used: set[str] = set()
    for lot in lots:
        key = lot.seller_key
        if not key:
            logger.info("skip: нет продавца (%s)", lot_slug(lot))
            continue
        if key in used:
            continue
        prev = seen_sellers.get(key)
        if prev is not None and now - float(prev) < SELLER_TTL:
            logger.info("skip dup seller: %s", key)
            continue
        if not is_russian_lot(lot):
            logger.info("skip non-ru: %s", key)
            continue
        if not is_free_dm_lot(lot):
            logger.info(
                "skip dm (%s): %s free=%s paid=%s",
                key,
                lot.free_dm,
                lot.paid_dm_stars,
            )
            continue
        used.add(key)
        out.append(lot)
    return out


TRACKER_VERSION = "2.1"


def _load_session_from_db() -> str:
    """Сессия из Neptun Parser (gifts.db) — тот же volume /app/data."""
    try:
        from db import GiftDB

        db = GiftDB()
        acc = db.get_active_account()
        if acc and str(acc.get("session") or "").strip():
            return str(acc["session"]).strip()
        for row in db.list_accounts():
            sess = str(row.get("session") or "").strip()
            if sess:
                return sess
    except Exception as exc:  # noqa: BLE001
        logger.debug("session from db: %s", exc)
    return ""


async def _load_session_string(cfg: Config) -> str:
    if cfg.session_string:
        return cfg.session_string
    path = Path(cfg.session_file)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    fallback = _load_session_from_db()
    if fallback:
        logger.info(
            "Сессия взята из gifts.db → сохраняю в %s", cfg.session_file
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fallback, encoding="utf-8")
    return fallback


async def _get_client(cfg: Config) -> TelegramClient:
    session_string = await _load_session_string(cfg)
    if session_string:
        client = TelegramClient(
            StringSession(session_string), cfg.api_id, cfg.api_hash
        )
        await client.connect()
        if await client.is_user_authorized():
            return client
        await client.disconnect()
        logger.warning(
            "Сессия в %s недействительна — нужен повторный вход",
            cfg.session_file,
        )

    logger.warning(
        "⚠️ Трекер v%s: нет сессии — постинг ОТКЛЮЧЁН до входа. "
        "Лоты в канале сейчас могут идти от старого деплоя.",
        TRACKER_VERSION,
    )
    import session_login

    return await session_login.bot_login_wizard(cfg)


async def run() -> None:
    _load_dotenv()
    cfg = Config.from_env()

    client = await _get_client(cfg)
    me = await client.get_me()
    logger.info(
        "✅ Трекер v%s запущен · %s (id=%s) · фильтры: RU + бесплатные ЛС",
        TRACKER_VERSION,
        me.username or me.first_name,
        me.id,
    )

    state_path = Path(cfg.state_file)
    state = load_state(state_path)
    seen: dict[str, float] = state["seen"]
    seen_sellers: dict[str, float] = state.get("seen_sellers", {})

    if state.get("channel_id"):
        chat_id = int(state["channel_id"])
    else:
        chat_id = await resolve_channel(client, cfg.target_channel)
        state["channel_id"] = chat_id
        save_state(state_path, state)
    logger.info("Канал для постинга: %s", chat_id)

    sender = Sender(cfg, client)
    sender.chat_id = chat_id

    m = TelegramMarket(client)
    gift_ids = await m.load_collections(force=True)
    if not gift_ids:
        raise SystemExit("Не удалось загрузить список коллекций подарков")
    logger.info("Коллекций с resale: %s", len(gift_ids))

    baseline = not seen and not cfg.post_on_first_run
    if baseline:
        logger.info("Первый запуск: запоминаю текущие лоты без постинга…")
        await poll_once(m, gift_ids, seen, cfg, baseline=True)
        save_state(state_path, state)
        logger.info("Запомнено %s лотов. Слежу за новыми.", len(seen))

    catalog_refreshed = time.monotonic()
    try:
        while True:
            started = time.monotonic()
            try:
                fresh = await poll_once(m, gift_ids, seen, cfg, baseline=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("Проход упал: %s", exc)
                await asyncio.sleep(3)
                continue

            if fresh:
                fresh.sort(key=lambda l: l.stars)
                await enrich(m, fresh)
                now = time.time()
                to_post = filter_for_post(fresh, seen_sellers, now=now)
                skipped = len(fresh) - len(to_post)
                if skipped:
                    logger.info(
                        "Проход: %s новых, к посту %s, отсеяно %s",
                        len(fresh),
                        len(to_post),
                        skipped,
                    )
                for lot in to_post:
                    try:
                        await sender.send(lot)
                        key = lot.seller_key
                        if key:
                            seen_sellers[key] = now
                        logger.info(
                            "Отправил: %s за %s⭐ lvl=%s (%s @%s)",
                            lot.title,
                            int(lot.stars),
                            format_account_level(lot),
                            lot_slug(lot),
                            lot.seller or "?",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Не отправилось (%s): %s", lot.id, exc)
                state["seen_sellers"] = seen_sellers
                save_state(state_path, state)

            # каталог коллекций обновляем раз в 10 минут (новые гифты)
            if time.monotonic() - catalog_refreshed > 600:
                try:
                    gift_ids = await m.load_collections(force=True)
                    catalog_refreshed = time.monotonic()
                except Exception:  # noqa: BLE001
                    pass

            spent = time.monotonic() - started
            await asyncio.sleep(max(cfg.poll_interval - spent, 0.2))
    finally:
        await sender.close()
        await client.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Остановлен.")


if __name__ == "__main__":
    main()
