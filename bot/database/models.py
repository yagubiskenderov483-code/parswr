from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    min_stars: Mapped[float] = mapped_column(Float, default=2000.0)
    max_stars: Mapped[float] = mapped_column(Float, default=100000.0)
    poll_interval: Mapped[float] = mapped_column(Float, default=2.0)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    market_telegram: Mapped[bool] = mapped_column(Boolean, default=True)
    market_portal: Mapped[bool] = mapped_column(Boolean, default=True)
    market_mrkt: Mapped[bool] = mapped_column(Boolean, default=True)
    market_tonnel: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SeenLot(Base):
    __tablename__ = "seen_lots"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_seen_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(191), index=True)
    market: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512), default="")
    price_stars: Mapped[float] = mapped_column(Float, default=0.0)
    original_price: Mapped[float] = mapped_column(Float, default=0.0)
    original_currency: Mapped[str] = mapped_column(String(16), default="STARS")
    difficulty: Mapped[str] = mapped_column(String(32), default="Custom")
    url: Mapped[str] = mapped_column(Text, default="")
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParseRun(Base):
    __tablename__ = "parse_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lots_found: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ton_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stars_usd: Mapped[float] = mapped_column(Float, default=0.013)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
