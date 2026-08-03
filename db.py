"""SQLite: модели гифтов + юзеры (AFK до 5M) + коллекции."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from market import Lot

DB_PATH = Path("data") / "gifts.db"
USER_CAP = 5_000_000


def _model_name(lot: Lot) -> str:
    return (lot.model or lot.title or "").strip()


def _model_key(lot: Lot) -> str:
    title = (lot.title or "").strip().lower()
    model = (lot.model or "").strip().lower()
    return f"{title}|{model}" if model else (title or lot.id)


class GiftDB:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gifts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                number INTEGER,
                stars REAL NOT NULL,
                slug TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                model_key TEXT NOT NULL DEFAULT '',
                backdrop TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                nft_url TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(gifts)").fetchall()
        }
        if "model_key" not in cols:
            self._conn.execute(
                "ALTER TABLE gifts ADD COLUMN model_key TEXT NOT NULL DEFAULT ''"
            )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                gift_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                last_offset TEXT NOT NULL DEFAULT '',
                pages INTEGER NOT NULL DEFAULT 0,
                lots_seen INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_stars ON gifts(stars)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_slug ON gifts(slug)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_model_key ON gifts(model_key)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        )
        self._conn.commit()

    def upsert_models(self, lots: Iterable[Lot]) -> tuple[int, int]:
        now = time.time()
        inserted = updated = 0
        cur = self._conn.cursor()
        for lot in lots:
            mk = _model_key(lot)
            model = _model_name(lot)
            row = cur.execute(
                "SELECT id FROM gifts WHERE id = ?", (lot.id,)
            ).fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO gifts (
                        id, title, number, stars, slug, model, model_key,
                        backdrop, symbol, nft_url, first_seen, last_seen, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lot.id,
                        lot.title,
                        lot.number,
                        float(lot.stars),
                        lot.slug,
                        model,
                        mk,
                        lot.backdrop,
                        lot.symbol,
                        lot.nft_url,
                        now,
                        now,
                        now,
                    ),
                )
                inserted += 1
            else:
                cur.execute(
                    """
                    UPDATE gifts SET
                        title=?, number=?, stars=?, slug=?, model=?, model_key=?,
                        backdrop=?, symbol=?, nft_url=?, last_seen=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        lot.title,
                        lot.number,
                        float(lot.stars),
                        lot.slug,
                        model,
                        mk,
                        lot.backdrop,
                        lot.symbol,
                        lot.nft_url,
                        now,
                        now,
                        lot.id,
                    ),
                )
                updated += 1
        self._conn.commit()
        return inserted, updated

    def upsert_lots(self, lots: Iterable[Lot]) -> tuple[int, int]:
        return self.upsert_models(lots)

    def upsert_users(
        self,
        users: Iterable[dict[str, Any]],
        *,
        cap: int = USER_CAP,
    ) -> tuple[int, int, int]:
        """Save users. Returns (inserted, updated, total). Stops inserting at cap."""
        now = time.time()
        inserted = updated = 0
        cur = self._conn.cursor()
        total = self.count_users()
        for u in users:
            uid = u.get("user_id")
            if uid is None:
                continue
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                continue
            username = str(u.get("username") or "").lstrip("@").strip()
            first_name = str(u.get("first_name") or "")
            last_name = str(u.get("last_name") or "")
            row = cur.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if row is None:
                if total >= cap:
                    continue
                cur.execute(
                    """
                    INSERT INTO users (
                        user_id, username, first_name, last_name, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uid, username, first_name, last_name, now, now),
                )
                inserted += 1
                total += 1
            else:
                cur.execute(
                    """
                    UPDATE users SET
                        username = CASE WHEN ? != '' THEN ? ELSE username END,
                        first_name = CASE WHEN ? != '' THEN ? ELSE first_name END,
                        last_name = CASE WHEN ? != '' THEN ? ELSE last_name END,
                        last_seen = ?
                    WHERE user_id = ?
                    """,
                    (
                        username,
                        username,
                        first_name,
                        first_name,
                        last_name,
                        last_name,
                        now,
                        uid,
                    ),
                )
                updated += 1
        self._conn.commit()
        return inserted, updated, total

    def upsert_users_from_lots(
        self, lots: Iterable[Lot], *, cap: int = USER_CAP
    ) -> tuple[int, int, int]:
        users = []
        for lot in lots:
            if lot.seller_id is None and not lot.seller:
                continue
            users.append(
                {
                    "user_id": lot.seller_id
                    if lot.seller_id is not None
                    else hash(lot.seller) & 0x7FFFFFFF,
                    "username": lot.seller,
                    "first_name": lot.first_name,
                    "last_name": lot.last_name,
                }
            )
        # без seller_id не надёжно — лучше только с id
        users = [u for u in users if u.get("user_id") is not None]
        return self.upsert_users(users, cap=cap)

    def touch_collection(
        self,
        gift_id: int,
        *,
        title: str = "",
        last_offset: str = "",
        pages_inc: int = 0,
        lots_inc: int = 0,
    ) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT gift_id, pages, lots_seen FROM collections WHERE gift_id = ?",
            (gift_id,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO collections (
                    gift_id, title, last_offset, pages, lots_seen, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (gift_id, title, last_offset, pages_inc, lots_inc, now),
            )
        else:
            self._conn.execute(
                """
                UPDATE collections SET
                    title = CASE WHEN ? != '' THEN ? ELSE title END,
                    last_offset = ?,
                    pages = pages + ?,
                    lots_seen = lots_seen + ?,
                    updated_at = ?
                WHERE gift_id = ?
                """,
                (title, title, last_offset, pages_inc, lots_inc, now, gift_id),
            )
        self._conn.commit()

    def get_collection_offset(self, gift_id: int) -> str:
        row = self._conn.execute(
            "SELECT last_offset FROM collections WHERE gift_id = ?", (gift_id,)
        ).fetchone()
        return str(row["last_offset"] if row else "") or ""

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM gifts").fetchone()
        return int(row["c"] if row else 0)

    def count_models(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT model_key) AS c FROM gifts WHERE model_key != ''"
        ).fetchone()
        return int(row["c"] if row else 0)

    def count_users(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"] if row else 0)

    def count_collections(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM collections").fetchone()
        return int(row["c"] if row else 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
