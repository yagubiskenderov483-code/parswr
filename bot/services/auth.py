from __future__ import annotations

import logging
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from bot.config import Settings, get_settings
from bot.parsers.registry import (
    build_parsers,
    build_telethon,
    fetch_mrkt_token,
    fetch_portals_auth,
    get_webapp_init_data,
)

logger = logging.getLogger(__name__)


class AuthService:
    """Interactive Telethon login driven by Telegram bot messages."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: TelegramClient | None = None
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.authorized_as: str | None = None
        self.mrkt_token: str = ""
        self.portals_auth: str = ""
        self.tonnel_auth: str = ""

    @property
    def session_file(self) -> Path:
        return Path(str(self.settings.session_path) + ".session")

    async def ensure_client(self) -> TelegramClient:
        # Always re-read settings (.env may be filled after start)
        self.settings = get_settings()
        self.settings.require_telethon()
        if self.client is None:
            self.client = build_telethon(self.settings)
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def is_authorized(self) -> bool:
        try:
            client = await self.ensure_client()
            ok = await client.is_user_authorized()
            if ok and not self.authorized_as:
                me = await client.get_me()
                self.authorized_as = _format_user(me)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auth check failed: %s", exc)
            return False

    async def send_code(self, phone: str) -> str:
        phone = _normalize_phone(phone)
        client = await self.ensure_client()
        try:
            result = await client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            raise ValueError("Неверный номер. Пример: +79991234567") from exc
        except FloodWaitError as exc:
            raise ValueError(f"Слишком много попыток. Подожди {exc.seconds} сек.") from exc
        self.phone = phone
        self.phone_code_hash = result.phone_code_hash
        return "Код отправлен в Telegram / SMS. Пришли его сюда."

    async def confirm_code(self, code: str) -> str:
        if not self.phone or not self.phone_code_hash:
            raise ValueError("Сначала отправь номер телефона.")
        client = await self.ensure_client()
        code = code.strip().replace(" ", "").replace("-", "")
        try:
            await client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return "NEED_PASSWORD"
        except PhoneCodeInvalidError as exc:
            raise ValueError("Неверный код. Попробуй ещё раз.") from exc
        except PhoneCodeExpiredError as exc:
            raise ValueError("Код истёк. Нажми /start и введи номер заново.") from exc
        await self._on_success()
        return "OK"

    async def confirm_password(self, password: str) -> str:
        client = await self.ensure_client()
        try:
            await client.sign_in(password=password.strip())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Пароль не принят: {exc}") from exc
        await self._on_success()
        return "OK"

    async def _on_success(self) -> None:
        client = await self.ensure_client()
        me = await client.get_me()
        self.authorized_as = _format_user(me)
        await self.refresh_market_tokens()
        logger.info("Telethon authorized as %s", self.authorized_as)

    async def refresh_market_tokens(self) -> None:
        client = await self.ensure_client()
        if not await client.is_user_authorized():
            return
        try:
            self.mrkt_token = await fetch_mrkt_token(client)
            logger.info("MRKT token ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MRKT token failed: %s", exc)
        try:
            self.portals_auth = await fetch_portals_auth(client)
            logger.info("Portal auth ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portal auth failed: %s", exc)
        try:
            self.tonnel_auth = await get_webapp_init_data(
                client, "tonnel_network_bot", "gifts"
            )
            logger.info("Tonnel auth ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tonnel auth failed: %s", exc)

    async def build_authorized_parsers(self):
        # Prefer freshly obtained tokens; env fallbacks handled inside build_parsers
        import os

        if self.mrkt_token:
            os.environ["MRKT_TOKEN"] = self.mrkt_token
        if self.portals_auth:
            os.environ["PORTALS_AUTH"] = self.portals_auth
        if self.tonnel_auth:
            os.environ["TONNEL_AUTH"] = self.tonnel_auth
        return await build_parsers(self.settings)


def _normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("8") and len(phone) == 11:
        phone = "+7" + phone[1:]
    if not phone.startswith("+"):
        phone = "+" + phone
    if not re.fullmatch(r"\+\d{10,15}", phone):
        raise ValueError("Номер должен быть в формате +79991234567")
    return phone


def _format_user(me) -> str:
    if getattr(me, "username", None):
        return f"@{me.username}"
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    return name or str(me.id)
