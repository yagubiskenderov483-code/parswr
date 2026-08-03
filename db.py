"""SQLite storage for parsed gift MODELS (not usernames)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from market import Lot

DB_PATH = Path("data") / "gifts.db"


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
        # Конкретные NFT-лоты (модели с номером)
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
        # На случай старой схемы — добавим model_key если нет
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(gifts)").fetchall()
        }
        if "model_key" not in cols:
            self._conn.execute(
                "ALTER TABLE gifts ADD COLUMN model_key TEXT NOT NULL DEFAULT ''"
            )
        # seller колонки могут остаться от старой версии — не используем как суть сейва
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_stars ON gifts(stars)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_slug ON gifts(slug)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gifts_model_key ON gifts(model_key)"
        )
        self._conn.commit()

    def upsert_models(self, lots: Iterable[Lot]) -> tuple[int, int]:
        """Сохраняет МОДЕЛИ/лоты. Юзернеймы не трогаем. Returns (inserted, updated)."""
        now = time.time()
        inserted = 0
        updated = 0
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
                        title = ?,
                        number = ?,
                        stars = ?,
                        slug = ?,
                        model = ?,
                        model_key = ?,
                        backdrop = ?,
                        symbol = ?,
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

    # alias for old call sites
    def upsert_lots(self, lots: Iterable[Lot]) -> tuple[int, int]:
        return self.upsert_models(lots)

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM gifts").fetchone()
        return int(row["c"] if row else 0)

    def count_models(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT model_key) AS c FROM gifts WHERE model_key != ''"
        ).fetchone()
        return int(row["c"] if row else 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
