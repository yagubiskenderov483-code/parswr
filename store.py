"""Disk caches: profiles, blacklist, listing freshness, collected usernames."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data"
PROFILES_PATH = DATA / "profile_cache.json"
BLACKLIST_PATH = DATA / "blacklist.json"
LISTINGS_PATH = DATA / "listings.json"
USERS_PATH = DATA / "found_users.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("load %s: %s", path.name, exc)
    return {}


def _save(path: Path, data: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


class Store:
    def __init__(self) -> None:
        DATA.mkdir(exist_ok=True)
        self.profiles: dict[str, Any] = _load(PROFILES_PATH)
        self.blacklist: dict[str, Any] = _load(BLACKLIST_PATH)
        self.listings: dict[str, Any] = _load(LISTINGS_PATH)
        self.found_users: dict[str, Any] = _load(USERS_PATH)
        self._dirty_profiles = False
        self._dirty_listings = False
        self._dirty_users = False

    # --- profiles ---
    def get_profile(self, key: int | str) -> dict[str, Any] | None:
        return self.profiles.get(str(key).lower() if isinstance(key, str) else str(key))

    def set_profile(self, key: int | str, value: dict[str, Any]) -> None:
        self.profiles[str(key).lower() if isinstance(key, str) else str(key)] = value
        self._dirty_profiles = True

    def save_profiles(self) -> None:
        if self._dirty_profiles:
            _save(PROFILES_PATH, self.profiles)
            self._dirty_profiles = False

    # --- blacklist ---
    def is_blocked(self, username: str | None = None, user_id: int | None = None) -> bool:
        if user_id is not None and str(user_id) in self.blacklist.get("ids", {}):
            return True
        if username:
            return username.lower().lstrip("@") in self.blacklist.get("users", {})
        return False

    def block(
        self,
        username: str | None = None,
        user_id: int | None = None,
        reason: str = "manual",
    ) -> None:
        users = self.blacklist.setdefault("users", {})
        ids = self.blacklist.setdefault("ids", {})
        now = time.time()
        if username:
            users[username.lower().lstrip("@")] = {"ts": now, "reason": reason}
        if user_id is not None:
            ids[str(user_id)] = {"ts": now, "reason": reason, "user": username or ""}
        _save(BLACKLIST_PATH, self.blacklist)

    def unblock(self, username: str) -> bool:
        users = self.blacklist.setdefault("users", {})
        key = username.lower().lstrip("@")
        if key in users:
            users.pop(key, None)
            _save(BLACKLIST_PATH, self.blacklist)
            return True
        return False

    # --- listing freshness ---
    def touch_listing(self, slug: str, price: float) -> float:
        """Возвращает age сек с первого наблюдения. 0 = только что."""
        if not slug:
            return 0.0
        now = time.time()
        row = self.listings.get(slug)
        if not row:
            self.listings[slug] = {"first": now, "last": now, "price": price}
            self._dirty_listings = True
            return 0.0
        first = float(row.get("first", now))
        row["last"] = now
        # смена цены ≈ новое выставление
        old_price = float(row.get("price", price))
        if abs(old_price - price) > 0.5:
            row["first"] = now
            row["price"] = price
            first = now
        self._dirty_listings = True
        return max(0.0, now - first)

    def is_fresh_listing(self, slug: str, price: float, max_age: float) -> bool:
        age = self.touch_listing(slug, price)
        return age <= max_age

    def save_listings(self) -> None:
        if not self._dirty_listings:
            return
        # чистим старше 24ч
        now = time.time()
        self.listings = {
            k: v
            for k, v in self.listings.items()
            if now - float(v.get("last", 0)) < 86400
        }
        _save(LISTINGS_PATH, self.listings)
        self._dirty_listings = False

    # --- found usernames ---
    def add_found(self, username: str, meta: dict[str, Any] | None = None) -> None:
        key = username.lower().lstrip("@")
        if not key:
            return
        self.found_users[key] = {
            "ts": time.time(),
            **(meta or {}),
        }
        self._dirty_users = True

    def save_users(self) -> None:
        if self._dirty_users:
            _save(USERS_PATH, self.found_users)
            self._dirty_users = False

    def export_usernames(self) -> str:
        names = sorted(self.found_users.keys())
        return "\n".join(f"@{n}" for n in names)

    def flush(self) -> None:
        self.save_profiles()
        self.save_listings()
        self.save_users()


store = Store()
