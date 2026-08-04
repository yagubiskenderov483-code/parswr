"""SQLite: модели гифтов + юзеры (AFK до 5M) + коллекции + блоклист."""

from __future__ import annotations

import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from market import Lot

# GIFTS_DB_PATH — для persistent volume на деплое
DB_PATH = Path(os.environ.get("GIFTS_DB_PATH") or (Path("data") / "gifts.db"))
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
                seller TEXT NOT NULL DEFAULT '',
                seller_id INTEGER,
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
        for col, decl in (
            ("model_key", "TEXT NOT NULL DEFAULT ''"),
            ("seller", "TEXT NOT NULL DEFAULT ''"),
            ("seller_id", "INTEGER"),
        ):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE gifts ADD COLUMN {col} {decl}")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                is_premium INTEGER,
                account_level INTEGER,
                gifts_count INTEGER,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        ucols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(users)").fetchall()
        }
        for col, decl in (
            ("is_premium", "INTEGER"),
            ("account_level", "INTEGER"),
            ("gifts_count", "INTEGER"),
        ):
            if col not in ucols:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

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
            """
            CREATE TABLE IF NOT EXISTS blocklist (
                key TEXT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        # уже показанные продавцы/модели — без повторов между рестартами
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_sellers (
                key TEXT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_models (
                model_key TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
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
            "CREATE INDEX IF NOT EXISTS idx_gifts_seller ON gifts(seller)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_username ON blocklist(username)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_user_id ON blocklist(user_id)"
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
                "SELECT id, seller, seller_id FROM gifts WHERE id = ?", (lot.id,)
            ).fetchone()
            seller = lot.seller or (row["seller"] if row else "") or ""
            seller_id = lot.seller_id
            if seller_id is None and row is not None:
                seller_id = row["seller_id"]
            if row is None:
                cur.execute(
                    """
                    INSERT INTO gifts (
                        id, title, number, stars, slug, model, model_key,
                        backdrop, symbol, nft_url, seller, seller_id,
                        first_seen, last_seen, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        seller,
                        seller_id,
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
                        backdrop=?, symbol=?, nft_url=?,
                        seller=CASE WHEN ? != '' THEN ? ELSE seller END,
                        seller_id=COALESCE(?, seller_id),
                        last_seen=?, updated_at=?
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
                        seller,
                        seller,
                        seller_id,
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
            premium = u.get("is_premium")
            level = u.get("account_level")
            gifts = u.get("gifts_count")
            row = cur.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            if row is None:
                if total >= cap:
                    continue
                cur.execute(
                    """
                    INSERT INTO users (
                        user_id, username, first_name, last_name,
                        is_premium, account_level, gifts_count,
                        first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        username,
                        first_name,
                        last_name,
                        None if premium is None else int(bool(premium)),
                        level,
                        gifts,
                        now,
                        now,
                    ),
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
                        is_premium = COALESCE(?, is_premium),
                        account_level = COALESCE(?, account_level),
                        gifts_count = COALESCE(?, gifts_count),
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
                        None if premium is None else int(bool(premium)),
                        level,
                        gifts,
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
            if lot.seller_id is None:
                continue
            users.append(
                {
                    "user_id": lot.seller_id,
                    "username": lot.seller,
                    "first_name": lot.first_name,
                    "last_name": lot.last_name,
                    "is_premium": lot.is_premium,
                    "account_level": lot.account_level,
                    "gifts_count": lot.gifts_count,
                }
            )
        return self.upsert_users(users, cap=cap)

    def fetch_random_lots(
        self,
        *,
        min_stars: float,
        max_stars: float,
        limit: int = 40,
        require_seller: bool = False,
    ) -> list[Lot]:
        """Старые лоты из БД в диапазоне цены (рандом) + профиль юзера если есть."""
        sql = """
            SELECT
                g.id, g.title, g.number, g.stars, g.slug, g.model,
                g.backdrop, g.symbol, g.nft_url, g.seller, g.seller_id,
                u.username AS u_username,
                u.first_name AS u_first_name,
                u.last_name AS u_last_name,
                u.is_premium AS u_is_premium,
                u.account_level AS u_account_level,
                u.gifts_count AS u_gifts_count
            FROM gifts g
            LEFT JOIN users u ON u.user_id = g.seller_id
            WHERE g.stars >= ? AND g.stars <= ?
        """
        params: list[Any] = [float(min_stars), float(max_stars)]
        if require_seller:
            sql += " AND (g.seller != '' OR IFNULL(u.username, '') != '')"
        sql += " ORDER BY RANDOM() LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        lots: list[Lot] = []
        for r in rows:
            seller = str(r["seller"] or "").lstrip("@").strip()
            if not seller:
                seller = str(r["u_username"] or "").lstrip("@").strip()
            premium = r["u_is_premium"]
            lot = Lot(
                id=str(r["id"]),
                title=str(r["title"] or "Gift"),
                number=r["number"],
                stars=float(r["stars"]),
                slug=str(r["slug"] or ""),
                model=str(r["model"] or ""),
                backdrop=str(r["backdrop"] or ""),
                symbol=str(r["symbol"] or ""),
                seller=seller,
                seller_id=r["seller_id"],
                first_name=str(r["u_first_name"] or ""),
                last_name=str(r["u_last_name"] or ""),
                is_premium=None if premium is None else bool(premium),
                account_level=r["u_account_level"],
                gifts_count=r["u_gifts_count"],
            )
            lots.append(lot)
        # разнообразие NFT: не отдавать пачку из одной коллекции подряд
        by_title: dict[str, list[Lot]] = {}
        for lot in lots:
            tk = (lot.title or lot.model or lot.id).strip().lower()
            by_title.setdefault(tk, []).append(lot)
        titles = list(by_title.keys())
        random.shuffle(titles)
        mixed: list[Lot] = []
        while any(by_title.values()):
            random.shuffle(titles)
            for tk in titles:
                bucket = by_title.get(tk) or []
                if bucket:
                    mixed.append(bucket.pop(random.randrange(len(bucket))))
        return mixed

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

    @staticmethod
    def _block_key(username: str = "", user_id: int | None = None) -> str:
        u = (username or "").lstrip("@").strip().lower()
        if u:
            return f"u:{u}"
        if user_id is not None:
            return f"id:{int(user_id)}"
        return ""

    def block_user(
        self,
        *,
        username: str = "",
        user_id: int | None = None,
        reason: str = "",
    ) -> bool:
        key = self._block_key(username, user_id)
        if not key:
            return False
        now = time.time()
        u = (username or "").lstrip("@").strip().lower()
        self._conn.execute(
            """
            INSERT INTO blocklist (key, username, user_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                username = CASE WHEN excluded.username != '' THEN excluded.username ELSE blocklist.username END,
                user_id = COALESCE(excluded.user_id, blocklist.user_id),
                reason = CASE WHEN excluded.reason != '' THEN excluded.reason ELSE blocklist.reason END
            """,
            (key, u, user_id, reason or "", now),
        )
        self._conn.commit()
        return True

    def unblock_user(self, *, username: str = "", user_id: int | None = None) -> bool:
        key = self._block_key(username, user_id)
        if not key:
            return False
        cur = self._conn.execute("DELETE FROM blocklist WHERE key = ?", (key,))
        if u := (username or "").lstrip("@").strip().lower():
            self._conn.execute(
                "DELETE FROM blocklist WHERE lower(username) = ?", (u,)
            )
        self._conn.commit()
        return cur.rowcount > 0

    def is_blocked(self, *, username: str = "", user_id: int | None = None) -> bool:
        u = (username or "").lstrip("@").strip().lower()
        if u:
            row = self._conn.execute(
                "SELECT 1 FROM blocklist WHERE key = ? OR lower(username) = ? LIMIT 1",
                (f"u:{u}", u),
            ).fetchone()
            if row:
                return True
        if user_id is not None:
            row = self._conn.execute(
                "SELECT 1 FROM blocklist WHERE user_id = ? OR key = ? LIMIT 1",
                (int(user_id), f"id:{int(user_id)}"),
            ).fetchone()
            if row:
                return True
        return False

    def list_blocked(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT username, user_id, reason, created_at
            FROM blocklist
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_blocked(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM blocklist").fetchone()
        return int(row["c"] if row else 0)

    def mark_seen_seller(
        self, *, username: str = "", user_id: int | None = None
    ) -> None:
        key = self._block_key(username, user_id)
        if not key:
            return
        now = time.time()
        u = (username or "").lstrip("@").strip().lower()
        self._conn.execute(
            """
            INSERT INTO seen_sellers (key, username, user_id, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                username = CASE WHEN excluded.username != '' THEN excluded.username ELSE seen_sellers.username END,
                user_id = COALESCE(excluded.user_id, seen_sellers.user_id),
                last_seen = excluded.last_seen
            """,
            (key, u, user_id, now, now),
        )
        self._conn.commit()

    def mark_seen_model(self, model_key: str, title: str = "") -> None:
        mk = (model_key or "").strip()
        if not mk:
            return
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO seen_models (model_key, title, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (mk, title or "", now, now),
        )
        self._conn.commit()

    def is_seen_seller(self, *, username: str = "", user_id: int | None = None) -> bool:
        key = self._block_key(username, user_id)
        if not key:
            return False
        u = (username or "").lstrip("@").strip().lower()
        row = self._conn.execute(
            "SELECT 1 FROM seen_sellers WHERE key = ? OR (? != '' AND username = ?) LIMIT 1",
            (key, u, u),
        ).fetchone()
        return row is not None

    def is_seen_model(self, model_key: str) -> bool:
        mk = (model_key or "").strip()
        if not mk:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM seen_models WHERE model_key = ? LIMIT 1", (mk,)
        ).fetchone()
        return row is not None

    def load_seen_seller_keys(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT key, username, user_id FROM seen_sellers"
        ).fetchall()
        out: set[str] = set()
        for r in rows:
            if r["key"]:
                out.add(str(r["key"]))
            u = str(r["username"] or "").lower()
            if u:
                out.add(u)
                out.add(f"u:{u}")
            if r["user_id"] is not None:
                out.add(f"id:{int(r['user_id'])}")
        return out

    def load_seen_model_keys(self) -> set[str]:
        rows = self._conn.execute("SELECT model_key FROM seen_models").fetchall()
        return {str(r["model_key"]) for r in rows if r["model_key"]}

    def load_block_keys(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT key, username, user_id FROM blocklist"
        ).fetchall()
        out: set[str] = set()
        for r in rows:
            if r["key"]:
                out.add(str(r["key"]))
            u = str(r["username"] or "").lower()
            if u:
                out.add(u)
                out.add(f"u:{u}")
            if r["user_id"] is not None:
                out.add(f"id:{int(r['user_id'])}")
        return out

    def checkpoint(self) -> None:
        """Слить WAL на диск — чтобы БД переживала рестарт/деплой."""
        try:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.checkpoint()
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
