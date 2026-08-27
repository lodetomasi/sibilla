"""Schema database (sez. 43) + tabelle di audit/journal/registry (sez. 35, 53, 62).

Compatibile PostgreSQL (produzione, con TimescaleDB opzionale) e SQLite (test/dev).
Tutti i timestamp sono timezone-aware UTC.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.clock import ensure_utc, utcnow


class UTCDateTime(TypeDecorator):
    """DateTime sempre timezone-aware UTC.

    SQLite non conserva il tzinfo: senza questo decoratore i timestamp riletti
    sarebbero naive e i confronti temporali (freshness, staleness, no-lookahead)
    esploderebbero a runtime.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _now() -> datetime:
    return utcnow()


TS = UTCDateTime()
Money = Numeric(18, 6, asdecimal=False)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TS, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now, nullable=False)


# --------------------------------------------------------------------------- #
# Eventi e mercati
# --------------------------------------------------------------------------- #
class Event(Base, TimestampMixin):
    """Nodo centrale dell'Event Knowledge Graph (sez. 15)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), index=True)
    participants: Mapped[list[Any]] = mapped_column(JSON, default=list)
    scheduled_at: Mapped[datetime | None] = mapped_column(TS, index=True)
    resolution_at: Mapped[datetime | None] = mapped_column(TS)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String(200))
    knowledge: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    markets: Mapped[list[Market]] = relationship(back_populates="event")


class Market(Base, TimestampMixin):
    """Mercato su un venue (polymarket | betfair | ...)."""

    __tablename__ = "markets"
    __table_args__ = (
        UniqueConstraint("venue", "external_id", name="uq_market_venue_external"),
        Index("ix_markets_status", "venue", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    venue: Mapped[str] = mapped_column(String(30), index=True)
    external_id: Mapped[str] = mapped_column(String(200), index=True)
    slug: Mapped[str | None] = mapped_column(String(300))
    question: Mapped[str] = mapped_column(Text)
    outcomes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    tradable: Mapped[bool] = mapped_column(Boolean, default=False)
    liquidity: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    created_date: Mapped[datetime | None] = mapped_column(TS)
    resolution_date: Mapped[datetime | None] = mapped_column(TS)
    resolution_source: Mapped[str | None] = mapped_column(Text)
    resolved_outcome: Mapped[str | None] = mapped_column(String(200))
    settlement_rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    event: Mapped[Event | None] = relationship(back_populates="markets")


class MarketMapping(Base, TimestampMixin):
    """Event matching cross-venue (sez. 14/66)."""

    __tablename__ = "market_mappings"
    __table_args__ = (
        UniqueConstraint("market_a_id", "market_b_id", name="uq_mapping_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_a_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    market_b_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome_a: Mapped[str] = mapped_column(String(200))
    outcome_b: Mapped[str] = mapped_column(String(200))
    similarity: Mapped[float] = mapped_column(Float)
    settlement_compatible: Mapped[bool] = mapped_column(Boolean, default=False)
    settlement_checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confirmed_by: Mapped[str] = mapped_column(String(40), default="auto")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MarketPrice(Base):
    """Serie storica prezzi (candidata a hypertable Timescale)."""

    __tablename__ = "market_prices"
    __table_args__ = (Index("ix_prices_market_ts", "market_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(200), default="YES")
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    price: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    liquidity: Mapped[float | None] = mapped_column(Float)
    total_matched: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), default="rest")


class OrderBookSnapshot(Base):
    """Snapshot book con depth per livello (sez. 8)."""

    __tablename__ = "orderbooks"
    __table_args__ = (Index("ix_books_market_ts", "market_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(200), default="YES")
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    bids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    asks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="OPEN")


class MarketAnomaly(Base):
    """Sez. 10 - anomaly detection."""

    __tablename__ = "market_anomalies"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(50))
    severity: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    investigated: Mapped[bool] = mapped_column(Boolean, default=False)
    investigation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Wallet intelligence
# --------------------------------------------------------------------------- #
class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(200))
    first_seen: Mapped[datetime | None] = mapped_column(TS)
    last_seen: Mapped[datetime | None] = mapped_column(TS, index=True)
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    n_markets: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WalletTrade(Base):
    __tablename__ = "wallet_trades"
    __table_args__ = (
        UniqueConstraint("venue", "external_id", name="uq_wallet_trade_external"),
        Index("ix_wallet_trades_addr_ts", "wallet_address", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(80), index=True)
    venue: Mapped[str] = mapped_column(String(30), default="polymarket")
    external_id: Mapped[str] = mapped_column(String(200))
    market_external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(200), index=True)
    asset_id: Mapped[str | None] = mapped_column(String(200))
    market_question: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    outcome: Mapped[str | None] = mapped_column(String(200))
    side: Mapped[str] = mapped_column(String(10), default="BUY")
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    usd_size: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WalletPosition(Base, TimestampMixin):
    __tablename__ = "wallet_positions"
    __table_args__ = (
        UniqueConstraint("wallet_address", "asset_id", name="uq_wallet_asset"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(80), index=True)
    asset_id: Mapped[str] = mapped_column(String(200))
    condition_id: Mapped[str | None] = mapped_column(String(200), index=True)
    market_question: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WalletScore(Base):
    """Score per wallet e categoria, valido a una data (sez. 5.2/5.3/6).

    `as_of` rende esplicito il point-in-time: nessun ranking usa dati futuri.
    """

    __tablename__ = "wallet_scores"
    __table_args__ = (
        UniqueConstraint("wallet_address", "category", "as_of", name="uq_wallet_score_period"),
        Index("ix_wallet_scores_cat_score", "category", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(80), index=True)
    category: Mapped[str] = mapped_column(String(40), default="ALL", index=True)
    as_of: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    window_start: Mapped[datetime | None] = mapped_column(TS)
    window_end: Mapped[datetime | None] = mapped_column(TS)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float | None] = mapped_column(Float)
    win_rate: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    persistence_score: Mapped[float | None] = mapped_column(Float)
    clv_edge: Mapped[float | None] = mapped_column(Float)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Informazione esterna
# --------------------------------------------------------------------------- #
class Source(Base, TimestampMixin):
    """Sez. 12 - registry fonti con tier e affidabilita appresa (sez. 60)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(200), index=True)
    source_type: Mapped[str] = mapped_column(String(40), default="media")
    tier: Mapped[str] = mapped_column(String(10), default="TIER_3")
    reliability: Mapped[float] = mapped_column(Float, default=0.7)
    categories: Mapped[list[Any]] = mapped_column(JSON, default=list)
    feed_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NewsItem(Base):
    """Sez. 11/12/13/67."""

    __tablename__ = "news"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_news_fingerprint"),
        Index("ix_news_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_name: Mapped[str] = mapped_column(String(200), index=True)
    source_type: Mapped[str] = mapped_column(String(40), default="media")
    tier: Mapped[str] = mapped_column(String(10), default="TIER_3")
    reliability: Mapped[float] = mapped_column(Float, default=0.7)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(10))
    published_at: Mapped[datetime | None] = mapped_column(TS, index=True)
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=_now)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_original: Mapped[bool] = mapped_column(Boolean, default=True)
    independent_confirmations: Mapped[int] = mapped_column(Integer, default=0)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    categories: Mapped[list[Any]] = mapped_column(JSON, default=list)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Segnali, decisioni LLM, esecuzione
# --------------------------------------------------------------------------- #
class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_ts_type", "ts", "signal_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    signal_type: Mapped[str] = mapped_column(String(60), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(60), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    polymarket_market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"), index=True)
    epic: Mapped[str | None] = mapped_column(String(80), index=True)
    direction: Mapped[str | None] = mapped_column(String(10))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expected_move_pct: Mapped[float | None] = mapped_column(Float)
    realized_move_pct: Mapped[float | None] = mapped_column(Float)
    residual_alpha_pct: Mapped[float | None] = mapped_column(Float)
    candidate_assets: Mapped[list[Any]] = mapped_column(JSON, default=list)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)


class LLMDecision(Base):
    """Sez. 36 - logging completo delle chiamate LLM."""

    __tablename__ = "llm_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    agent: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40), default="v1")
    tools_used: Mapped[list[Any]] = mapped_column(JSON, default=list)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    structured_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)


class Instrument(Base, TimestampMixin):
    """Patch sez. 4 - Instrument Registry (EPIC = identificatore IG)."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    epic: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    asset_class: Mapped[str] = mapped_column(String(30), default="OTHER", index=True)
    currency: Mapped[str] = mapped_column(String(10), default="EUR")
    market_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    min_size: Mapped[float] = mapped_column(Float, default=0.1)
    size_step: Mapped[float] = mapped_column(Float, default=0.1)
    lot_size: Mapped[float] = mapped_column(Float, default=1.0)
    contract_size: Mapped[float] = mapped_column(Float, default=1.0)
    value_per_point: Mapped[float] = mapped_column(Float, default=1.0)
    margin_factor: Mapped[float] = mapped_column(Float, default=5.0)
    spread: Mapped[float | None] = mapped_column(Float)
    trading_hours: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    controlled_risk_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    min_stop_distance: Mapped[float | None] = mapped_column(Float)
    max_stop_distance: Mapped[float | None] = mapped_column(Float)
    scaling_factor: Mapped[float] = mapped_column(Float, default=1.0)
    expiry: Mapped[str] = mapped_column(String(20), default="-")
    streaming_available: Mapped[bool] = mapped_column(Boolean, default=True)
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    factors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fallback_symbol: Mapped[str | None] = mapped_column(String(40))
    commission_pct: Mapped[float] = mapped_column(Float, default=0.0)
    overnight_funding_pct_annual: Mapped[float] = mapped_column(Float, default=0.03)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TS)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class InstrumentPrice(Base):
    """Serie prezzi degli strumenti IG (bid/offer), fonte esplicita (iron rule: nessun prezzo inventato)."""

    __tablename__ = "instrument_prices"
    __table_args__ = (Index("ix_instr_prices_epic_ts", "epic", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    epic: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    bid: Mapped[float] = mapped_column(Float)
    offer: Mapped[float] = mapped_column(Float)
    mid: Mapped[float] = mapped_column(Float)
    market_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    source: Mapped[str] = mapped_column(String(30), default="ig-rest")
    volume: Mapped[float | None] = mapped_column(Float)


class DetectedEventRecord(Base, TimestampMixin):
    """Eventi rilevati (patch sez. 38: EVENT DETECTION)."""

    __tablename__ = "detected_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    detected_at: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(TS)
    source_reliability: Mapped[float] = mapped_column(Float, default=0.0)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    surprise: Mapped[float | None] = mapped_column(Float)
    polymarket_probability_change: Mapped[float | None] = mapped_column(Float)
    polymarket_market_id: Mapped[str | None] = mapped_column(String(200))
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    impact_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MacroReleaseRecord(Base):
    """Patch sez. 31.D - calendario macro con actual/consensus/previous."""

    __tablename__ = "macro_releases"
    __table_args__ = (
        UniqueConstraint("indicator", "country", "release_time", name="uq_macro_release"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(10), default="US")
    release_time: Mapped[datetime] = mapped_column(TS, index=True)
    actual: Mapped[float | None] = mapped_column(Float)
    consensus: Mapped[float | None] = mapped_column(Float)
    previous: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str | None] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=_now)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base, TimestampMixin):
    """Ordini inviati (o simulati) - patch sez. 10/24."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_ts", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    deal_reference: Mapped[str | None] = mapped_column(String(120), index=True)
    deal_id: Mapped[str | None] = mapped_column(String(120), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(60), index=True)
    venue: Mapped[str] = mapped_column(String(30), default="ig")
    environment: Mapped[str] = mapped_column(String(10), default="DEMO")
    epic: Mapped[str] = mapped_column(String(80), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    entry_type: Mapped[str] = mapped_column(String(10), default="MARKET")
    size: Mapped[float] = mapped_column(Money)
    reference_price: Mapped[float] = mapped_column(Float)
    max_entry: Mapped[float | None] = mapped_column(Float)
    level: Mapped[float | None] = mapped_column(Float)
    fill_price: Mapped[float | None] = mapped_column(Float)
    filled_size: Mapped[float] = mapped_column(Money, default=0.0)
    slippage_pct: Mapped[float | None] = mapped_column(Float)
    stop_distance: Mapped[float | None] = mapped_column(Float)
    limit_distance: Mapped[float | None] = mapped_column(Float)
    stop_level: Mapped[float | None] = mapped_column(Float)
    limit_level: Mapped[float | None] = mapped_column(Float)
    risk_eur: Mapped[float] = mapped_column(Money, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    mode: Mapped[str] = mapped_column(String(20), default="SHADOW")
    purpose: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | CLOSE | AMEND
    reason_code: Mapped[str | None] = mapped_column(String(60))
    reason: Mapped[str | None] = mapped_column(Text)
    risk_checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confirmation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Money)
    commission: Mapped[float] = mapped_column(Money, default=0.0)
    slippage_pct: Mapped[float | None] = mapped_column(Float)
    deal_id: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(30), default="ig")


class Position(Base, TimestampMixin):
    """Posizioni (patch sez. 10, 14-18, 24)."""

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_status_mode", "status", "mode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    deal_id: Mapped[str | None] = mapped_column(String(120), index=True)
    deal_reference: Mapped[str | None] = mapped_column(String(120))
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(60), index=True)
    venue: Mapped[str] = mapped_column(String(30), default="ig")
    environment: Mapped[str] = mapped_column(String(10), default="DEMO")
    epic: Mapped[str] = mapped_column(String(80), index=True)
    instrument_name: Mapped[str | None] = mapped_column(String(200))
    asset_class: Mapped[str | None] = mapped_column(String(30))
    currency: Mapped[str] = mapped_column(String(10), default="EUR")
    direction: Mapped[str] = mapped_column(String(10))
    size: Mapped[float] = mapped_column(Money, default=0.0)
    value_per_point: Mapped[float] = mapped_column(Float, default=1.0)
    entry_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float)
    stop_level: Mapped[float | None] = mapped_column(Float)
    limit_level: Mapped[float | None] = mapped_column(Float)
    stop_distance: Mapped[float | None] = mapped_column(Float)
    limit_distance: Mapped[float | None] = mapped_column(Float)
    risk_eur: Mapped[float] = mapped_column(Money, default=0.0)
    notional: Mapped[float] = mapped_column(Money, default=0.0)
    margin_required: Mapped[float] = mapped_column(Money, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Money, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Money, default=0.0)
    commission_paid: Mapped[float] = mapped_column(Money, default=0.0)
    financing_paid: Mapped[float] = mapped_column(Money, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_CONFIRMATION")
    opened_at: Mapped[datetime] = mapped_column(TS, default=_now)
    max_holding_until: Mapped[datetime | None] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(40))
    invalidation_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    exit_criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    factors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="SHADOW")
    reconciled_at: Mapped[datetime | None] = mapped_column(TS)
    reconciliation_status: Mapped[str] = mapped_column(String(30), default="PENDING")


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="SHADOW")
    balance: Mapped[float] = mapped_column(Money)
    equity: Mapped[float] = mapped_column(Money)
    margin_used: Mapped[float] = mapped_column(Money, default=0.0)
    free_margin: Mapped[float] = mapped_column(Money, default=0.0)
    open_risk: Mapped[float] = mapped_column(Money, default=0.0)
    open_notional: Mapped[float] = mapped_column(Money, default=0.0)
    realized_pnl_day: Mapped[float] = mapped_column(Money, default=0.0)
    realized_pnl_total: Mapped[float] = mapped_column(Money, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Money, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    daily_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    weekly_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    peak_equity: Mapped[float | None] = mapped_column(Money)
    factor_exposure: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(30), default="paper")


class TradeJournalEntry(Base, TimestampMixin):
    """Sez. 35 + patch sez. 10/33/35 - trade journal completo e riproducibile."""

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="SHADOW", index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(60), index=True)
    signal_type: Mapped[str | None] = mapped_column(String(60))
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_title: Mapped[str | None] = mapped_column(Text)
    epic: Mapped[str | None] = mapped_column(String(80), index=True)
    instrument_name: Mapped[str | None] = mapped_column(String(200))
    direction: Mapped[str | None] = mapped_column(String(10))
    entry_price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Money)
    risk_eur: Mapped[float | None] = mapped_column(Money)
    stop_level: Mapped[float | None] = mapped_column(Float)
    limit_level: Mapped[float | None] = mapped_column(Float)
    time_horizon_seconds: Mapped[int | None] = mapped_column(Integer)
    expected_move_pct: Mapped[float | None] = mapped_column(Float)
    realized_move_pct: Mapped[float | None] = mapped_column(Float)
    residual_alpha_pct: Mapped[float | None] = mapped_column(Float)
    net_alpha_pct: Mapped[float | None] = mapped_column(Float)
    costs_pct: Mapped[float | None] = mapped_column(Float)
    probability: Mapped[float | None] = mapped_column(Float)
    llm_probability: Mapped[float | None] = mapped_column(Float)
    calibrated_probability: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    impact_map: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analyst_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    critic_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    portfolio_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation: Mapped[list[Any]] = mapped_column(JSON, default=list)
    latencies: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    invalidation_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    outcome: Mapped[str | None] = mapped_column(String(40))  # APPROVED | REJECTED_* | CLOSED_*
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(40))
    pnl: Mapped[float | None] = mapped_column(Money)
    post_signal_alpha: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    price_source: Mapped[str | None] = mapped_column(String(30))
    reproducible_inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Evaluation(Base):
    """Sez. 57/58/59 - metriche periodiche, attribution, ablation."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    scope: Mapped[str] = mapped_column(String(80), default="global")
    window_start: Mapped[datetime | None] = mapped_column(TS)
    window_end: Mapped[datetime | None] = mapped_column(TS)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)


class Strategy(Base, TimestampMixin):
    """Sez. 62 - strategy registry."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="RESEARCH", index=True)
    capital_limit: Mapped[float] = mapped_column(Money, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    backtest_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    paper_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    live_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    """Sez. 53 - ogni cambiamento e auditabile."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80))
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text)


class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    reason: Mapped[str] = mapped_column(String(60), index=True)
    triggered_by: Mapped[str] = mapped_column(String(80), default="system")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cleared_at: Mapped[datetime | None] = mapped_column(TS)
    cleared_by: Mapped[str | None] = mapped_column(String(80))


class CostRecord(Base):
    """Sez. 40 - cost control."""

    __tablename__ = "costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str | None] = mapped_column(String(80))
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    units: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Alert(Base):
    """Sez. 51."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="INFO")
    title: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    channels: Mapped[list[Any]] = mapped_column(JSON, default=list)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CalibrationRecord(Base):
    """Sez. 37 - calibrazione probabilita (per agente/dominio)."""

    __tablename__ = "calibration"
    __table_args__ = (
        UniqueConstraint("scope", "bucket", "as_of", name="uq_calibration_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    as_of: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    scope: Mapped[str] = mapped_column(String(80), default="llm_analyst", index=True)
    bucket: Mapped[str] = mapped_column(String(20))
    predicted_mean: Mapped[float] = mapped_column(Float)
    observed_rate: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    adjustment: Mapped[float] = mapped_column(Float, default=0.0)


class Prediction(Base):
    """Sez. 6 di principio (ogni previsione confrontabile ex post)."""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, default=_now, index=True)
    scope: Mapped[str] = mapped_column(String(80), index=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    epic: Mapped[str | None] = mapped_column(String(80))
    direction: Mapped[str | None] = mapped_column(String(10))
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    predicted_probability: Mapped[float] = mapped_column(Float)
    expected_move_pct: Mapped[float | None] = mapped_column(Float)
    realized_move_pct: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    realized_outcome: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SystemStateRecord(Base):
    """Stato operativo persistito (PAUSE/STOP/kill switch, sez. 71)."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(80), default="system")


ALL_TABLES = [
    Event, Market, MarketMapping, MarketPrice, OrderBookSnapshot, MarketAnomaly,
    Wallet, WalletTrade, WalletPosition, WalletScore, Source, NewsItem, Signal,
    LLMDecision, Instrument, InstrumentPrice, DetectedEventRecord, MacroReleaseRecord,
    Order, Fill, Position, PortfolioSnapshot, TradeJournalEntry,
    Evaluation, Strategy, AuditLog, KillSwitchEvent, CostRecord, Alert,
    CalibrationRecord, Prediction, SystemStateRecord,
]
