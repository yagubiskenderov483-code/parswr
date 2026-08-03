"""SQLite storage for all parsed gifts."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from market import Lot

DB_PATH = Path("data") / "gifts.db"


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
                backdrop TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                seller TEXT NOT NULL DEFAULT '',
                seller_id INTEGER,
                nft_url TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_stars ON gifts(stars)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_seller ON gifts(seller)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_slug ON gifts(slug)"
        )
        self._conn.commit()

    def upsert_lots(self, lots: Iterable[Lot]) -> tuple[int, int]:
        """Save/update gifts. Returns (inserted, updated)."""
        now = time.time()
        inserted = 0
        updated = 0
        cur = self._conn.cursor()
        for lot in lots:
            row = cur.execute(
                "SELECT id, seller FROM gifts WHERE id = ?", (lot.id,)
            ).fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO gifts (
                        id, title, number, stars, slug, model, backdrop, symbol,
                        seller, seller_id, nft_url, first_seen, last_seen, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lot.id,
                        lot.title,
                        lot.number,
                        float(lot.stars),
                        lot.slug,
                        lot.model,
                        lot.backdrop,
                        lot.symbol,
                        lot.seller or "",
                        lot.seller_id,
                        lot.nft_url,
                        now,
                        now,
                        now,
                    ),
                )
                inserted += 1
            else:
                seller = lot.seller or row["seller"] or ""
                cur.execute(
                    """
                    UPDATE gifts SET
                        title = ?,
                        number = ?,
                        stars = ?,
                        slug = ?,
                        model = ?,
                        backdrop = ?,
                        symbol = ?,
                        seller = ?,
                        seller_id = COALESCE(?, seller_id),
                        nft_url = ?,
                        last_seen = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        lot.title,
                        lot.number,
                        float(lot.stars),
                        lot.slug,
                        lot.model,
                        lot.backdrop,
                        lot.symbol,
                        seller,
                        lot.seller_id,
                        lot.nft_url,
                        now,
                        now,
                        lot.id,
                    ),
                )
                updated += 1
        self._conn.commit()
        return inserted, updated

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM gifts").fetchone()
        return int(row["c"] if row else 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
