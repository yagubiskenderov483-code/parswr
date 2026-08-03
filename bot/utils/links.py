from __future__ import annotations

import re


def nft_slug(title: str, number: int | None) -> str | None:
    if number is None:
        return None
    base = re.sub(r"[^A-Za-z0-9]+", "", title or "")
    if not base:
        return None
    return f"{base}-{int(number)}"


def nft_url(title: str, number: int | None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    slug = nft_slug(title, number)
    if slug:
        return f"https://t.me/nft/{slug}"
    return "https://t.me/nft/"


def seller_link(username: str | None, user_id: int | None = None) -> str | None:
    if username:
        uname = username.lstrip("@")
        return f"https://t.me/{uname}"
    if user_id:
        return f"tg://user?id={user_id}"
    return None


def write_url(
    *,
    seller_username: str | None,
    seller_id: int | None,
    market_url: str,
) -> str:
    link = seller_link(seller_username, seller_id)
    return link or market_url
