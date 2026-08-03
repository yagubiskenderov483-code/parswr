from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from telethon import TelegramClient
from telethon.tl.functions.payments import GetUniqueStarGiftRequest

from bot.utils.links import nft_slug

logger = logging.getLogger(__name__)


class OwnerResolver:
    """Resolve gift owner username via Telethon unique-gift lookup."""

    def __init__(self, client: TelegramClient | None = None) -> None:
        self.client = client
        self._cache: dict[str, str] = {}

    def set_client(self, client: TelegramClient | None) -> None:
        self.client = client

    async def resolve(
        self,
        *,
        title: str,
        number: int | None,
        nft_url: str = "",
        current_username: str = "",
    ) -> str:
        if current_username:
            return current_username.lstrip("@")

        slug = _slug_from_nft_url(nft_url) or nft_slug(title, number)
        if not slug:
            return ""
        if slug in self._cache:
            return self._cache[slug]

        username = await self._fetch_owner(slug)
        if username:
            self._cache[slug] = username
        return username

    async def _fetch_owner(self, slug: str) -> str:
        if self.client is None:
            return ""
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                return ""

            result = await self.client(GetUniqueStarGiftRequest(slug=slug))
            gift = getattr(result, "gift", result)

            # Prefer explicit owner username/name
            owner_name = getattr(gift, "owner_name", None)
            if owner_name and str(owner_name).startswith("@"):
                return str(owner_name).lstrip("@")
            if owner_name and re.fullmatch(r"[A-Za-z0-9_]{4,}", str(owner_name)):
                return str(owner_name)

            owner_id = getattr(gift, "owner_id", None)
            if owner_id is not None:
                try:
                    entity = await self.client.get_entity(owner_id)
                    uname = getattr(entity, "username", None)
                    if uname:
                        return str(uname)
                    # fallback display name
                    first = getattr(entity, "first_name", "") or ""
                    last = getattr(entity, "last_name", "") or ""
                    full = f"{first} {last}".strip()
                    if full:
                        return full
                except Exception as exc:  # noqa: BLE001
                    logger.debug("owner entity resolve failed for %s: %s", slug, exc)

            if owner_name:
                return str(owner_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("GetUniqueStarGift(%s) failed: %s", slug, exc)
        return ""


def _slug_from_nft_url(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"t\.me/nft/([^/?#]+)", unquote(url))
    return match.group(1) if match else None
