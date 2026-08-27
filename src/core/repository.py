"""Repository: accesso dati riusabile per worker, API e agenti.

Gli agenti LLM leggono SOLO tramite questi metodi (nessun SQL arbitrario, sez. 21).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import utcnow
from core.enums import OrderStatus, PositionStatus
from core.models import (
    Alert,
    AuditLog,
    CalibrationRecord,
    CostRecord,
    DetectedEventRecord,
    Evaluation,
    Event,
    Fill,
    Instrument,
    InstrumentPrice,
    KillSwitchEvent,
    LLMDecision,
    MacroReleaseRecord,
    Market,
    MarketAnomaly,
    MarketMapping,
    MarketPrice,
    NewsItem,
    Order,
    OrderBookSnapshot,
    PortfolioSnapshot,
    Position,
    Prediction,
    Signal,
    Source,
    Strategy,
    SystemStateRecord,
    TradeJournalEntry,
    Wallet,
    WalletPosition,
    WalletScore,
    WalletTrade,
)

OPEN_POSITION_STATES = (
    PositionStatus.PENDING_CONFIRMATION.value,
    PositionStatus.OPEN.value,
    PositionStatus.REDUCED.value,
    PositionStatus.CLOSING.value,
)


def _apply(obj: Any, values: dict[str, Any], *, skip_none: bool = True) -> None:
    for key, value in values.items():
        if skip_none and value is None:
            continue
        setattr(obj, key, value)


class Repository:
    """Wrapper attorno a una AsyncSession con query di dominio."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ---------------- polymarket: events / markets ----------------
    async def upsert_event(self, **kwargs: Any) -> Event:
        existing = await self.session.scalar(select(Event).where(Event.slug == kwargs["slug"]))
        if existing:
            _apply(existing, kwargs)
            return existing
        event = Event(**kwargs)
        self.session.add(event)
        await self.session.flush()
        return event

    async def upsert_market(self, venue: str, external_id: str, **kwargs: Any) -> Market:
        existing = await self.session.scalar(
            select(Market).where(Market.venue == venue, Market.external_id == external_id)
        )
        if existing:
            _apply(existing, kwargs)
            return existing
        market = Market(venue=venue, external_id=external_id, **kwargs)
        self.session.add(market)
        await self.session.flush()
        return market

    async def get_market(self, venue: str, external_id: str) -> Market | None:
        return await self.session.scalar(
            select(Market).where(Market.venue == venue, Market.external_id == external_id)
        )

    async def get_market_by_id(self, market_id: int) -> Market | None:
        return await self.session.get(Market, market_id)

    async def list_markets(
        self,
        *,
        venue: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> Sequence[Market]:
        stmt = select(Market)
        if venue:
            stmt = stmt.where(Market.venue == venue)
        if category:
            stmt = stmt.where(Market.category == category)
        if status:
            stmt = stmt.where(Market.status == status)
        return (await self.session.scalars(stmt.order_by(desc(Market.volume)).limit(limit))).all()

    async def search_markets(self, text: str, *, limit: int = 20) -> Sequence[Market]:
        pattern = f"%{text.lower()}%"
        stmt = (
            select(Market)
            .where(func.lower(Market.question).like(pattern))
            .order_by(desc(Market.volume))
            .limit(limit)
        )
        return (await self.session.scalars(stmt)).all()

    async def add_price(self, **kwargs: Any) -> MarketPrice:
        price = MarketPrice(**kwargs)
        self.session.add(price)
        return price

    async def price_history(
        self,
        market_id: int,
        *,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> Sequence[MarketPrice]:
        stmt = select(MarketPrice).where(MarketPrice.market_id == market_id)
        if outcome:
            stmt = stmt.where(MarketPrice.outcome == outcome)
        if since:
            stmt = stmt.where(MarketPrice.ts >= since)
        if until:
            stmt = stmt.where(MarketPrice.ts <= until)
        return (await self.session.scalars(stmt.order_by(MarketPrice.ts).limit(limit))).all()

    async def latest_price(self, market_id: int, outcome: str | None = None) -> MarketPrice | None:
        stmt = select(MarketPrice).where(MarketPrice.market_id == market_id)
        if outcome:
            stmt = stmt.where(MarketPrice.outcome == outcome)
        return await self.session.scalar(stmt.order_by(desc(MarketPrice.ts)).limit(1))

    async def add_orderbook(self, **kwargs: Any) -> OrderBookSnapshot:
        snapshot = OrderBookSnapshot(**kwargs)
        self.session.add(snapshot)
        return snapshot

    async def latest_orderbook(self, market_id: int, outcome: str | None = None):
        stmt = select(OrderBookSnapshot).where(OrderBookSnapshot.market_id == market_id)
        if outcome:
            stmt = stmt.where(OrderBookSnapshot.outcome == outcome)
        return await self.session.scalar(stmt.order_by(desc(OrderBookSnapshot.ts)).limit(1))

    async def add_anomaly(self, **kwargs: Any) -> MarketAnomaly:
        anomaly = MarketAnomaly(**kwargs)
        self.session.add(anomaly)
        await self.session.flush()
        return anomaly

    async def recent_anomalies(self, *, minutes: int = 60, limit: int = 100):
        return (
            await self.session.scalars(
                select(MarketAnomaly)
                .where(MarketAnomaly.ts >= utcnow() - timedelta(minutes=minutes))
                .order_by(desc(MarketAnomaly.ts))
                .limit(limit)
            )
        ).all()

    async def add_mapping(self, **kwargs: Any) -> MarketMapping:
        existing = await self.session.scalar(
            select(MarketMapping).where(
                MarketMapping.market_a_id == kwargs["market_a_id"],
                MarketMapping.market_b_id == kwargs["market_b_id"],
            )
        )
        if existing:
            _apply(existing, kwargs, skip_none=False)
            return existing
        mapping = MarketMapping(**kwargs)
        self.session.add(mapping)
        await self.session.flush()
        return mapping

    # ---------------- wallet ----------------
    async def upsert_wallet(self, address: str, **kwargs: Any) -> Wallet:
        existing = await self.session.scalar(select(Wallet).where(Wallet.address == address))
        if existing:
            _apply(existing, kwargs)
            return existing
        wallet = Wallet(address=address, **kwargs)
        self.session.add(wallet)
        await self.session.flush()
        return wallet

    async def get_wallet(self, address: str) -> Wallet | None:
        return await self.session.scalar(select(Wallet).where(Wallet.address == address))

    async def list_wallets(self, *, limit: int = 1000) -> Sequence[Wallet]:
        return (
            await self.session.scalars(
                select(Wallet).order_by(desc(Wallet.total_volume)).limit(limit)
            )
        ).all()

    async def add_wallet_trades(self, trades: list[dict[str, Any]]) -> int:
        if not trades:
            return 0
        external_ids = [t["external_id"] for t in trades]
        result = await self.session.execute(
            select(WalletTrade.venue, WalletTrade.external_id).where(
                WalletTrade.external_id.in_(external_ids)
            )
        )
        existing = {(row[0], row[1]) for row in result.all()}
        inserted = 0
        for trade in trades:
            key = (trade["venue"], trade["external_id"])
            if key in existing:
                continue
            self.session.add(WalletTrade(**trade))
            existing.add(key)
            inserted += 1
        return inserted

    async def wallet_trades(
        self,
        address: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        category: str | None = None,
        limit: int = 5000,
    ) -> Sequence[WalletTrade]:
        stmt = select(WalletTrade).where(WalletTrade.wallet_address == address)
        if since:
            stmt = stmt.where(WalletTrade.ts >= since)
        if until:
            stmt = stmt.where(WalletTrade.ts <= until)
        if category:
            stmt = stmt.where(WalletTrade.category == category)
        return (await self.session.scalars(stmt.order_by(WalletTrade.ts).limit(limit))).all()

    async def recent_wallet_trades(
        self, *, minutes: int = 30, addresses: list[str] | None = None, limit: int = 2000
    ) -> Sequence[WalletTrade]:
        stmt = select(WalletTrade).where(WalletTrade.ts >= utcnow() - timedelta(minutes=minutes))
        if addresses:
            stmt = stmt.where(WalletTrade.wallet_address.in_(addresses))
        return (await self.session.scalars(stmt.order_by(desc(WalletTrade.ts)).limit(limit))).all()

    async def upsert_wallet_position(self, address: str, asset_id: str, **kwargs: Any):
        existing = await self.session.scalar(
            select(WalletPosition).where(
                WalletPosition.wallet_address == address, WalletPosition.asset_id == asset_id
            )
        )
        if existing:
            _apply(existing, kwargs)
            return existing
        position = WalletPosition(wallet_address=address, asset_id=asset_id, **kwargs)
        self.session.add(position)
        return position

    async def wallet_positions(self, address: str) -> Sequence[WalletPosition]:
        return (
            await self.session.scalars(
                select(WalletPosition).where(WalletPosition.wallet_address == address)
            )
        ).all()

    async def save_wallet_score(self, **kwargs: Any) -> WalletScore:
        existing = await self.session.scalar(
            select(WalletScore).where(
                WalletScore.wallet_address == kwargs["wallet_address"],
                WalletScore.category == kwargs.get("category", "ALL"),
                WalletScore.as_of == kwargs["as_of"],
            )
        )
        if existing:
            _apply(existing, kwargs, skip_none=False)
            return existing
        score = WalletScore(**kwargs)
        self.session.add(score)
        return score

    async def top_wallets(
        self,
        *,
        category: str = "ALL",
        as_of: datetime | None = None,
        min_sample: int = 20,
        limit: int = 50,
    ) -> Sequence[WalletScore]:
        """Ranking point-in-time: usa solo score calcolati prima di `as_of` (sez. 6)."""
        stmt = select(WalletScore).where(
            WalletScore.category == category, WalletScore.sample_size >= min_sample
        )
        if as_of:
            stmt = stmt.where(WalletScore.as_of <= as_of)
        stmt = stmt.order_by(desc(WalletScore.as_of), desc(WalletScore.score)).limit(limit)
        return (await self.session.scalars(stmt)).all()

    async def wallet_score(
        self, address: str, category: str = "ALL", as_of: datetime | None = None
    ) -> WalletScore | None:
        stmt = select(WalletScore).where(
            WalletScore.wallet_address == address, WalletScore.category == category
        )
        if as_of:
            stmt = stmt.where(WalletScore.as_of <= as_of)
        return await self.session.scalar(stmt.order_by(desc(WalletScore.as_of)).limit(1))

    # ---------------- news / sources ----------------
    async def upsert_source(self, name: str, **kwargs: Any) -> Source:
        existing = await self.session.scalar(select(Source).where(Source.name == name))
        if existing:
            _apply(existing, kwargs)
            return existing
        source = Source(name=name, **kwargs)
        self.session.add(source)
        await self.session.flush()
        return source

    async def get_source(self, name: str) -> Source | None:
        return await self.session.scalar(select(Source).where(Source.name == name))

    async def list_sources(self, active_only: bool = True) -> Sequence[Source]:
        stmt = select(Source)
        if active_only:
            stmt = stmt.where(Source.active.is_(True))
        return (await self.session.scalars(stmt)).all()

    async def add_news(self, items: list[dict[str, Any]]) -> list[NewsItem]:
        if not items:
            return []
        fingerprints = [i["fingerprint"] for i in items]
        existing = set(
            (
                await self.session.scalars(
                    select(NewsItem.fingerprint).where(NewsItem.fingerprint.in_(fingerprints))
                )
            ).all()
        )
        created: list[NewsItem] = []
        for item in items:
            if item["fingerprint"] in existing:
                continue
            news = NewsItem(**item)
            self.session.add(news)
            created.append(news)
            existing.add(item["fingerprint"])
        await self.session.flush()
        return created

    async def recent_news(
        self,
        *,
        minutes: int = 120,
        category: str | None = None,
        min_reliability: float = 0.0,
        query: str | None = None,
        limit: int = 200,
    ) -> Sequence[NewsItem]:
        cutoff = utcnow() - timedelta(minutes=minutes)
        ts_col = func.coalesce(NewsItem.published_at, NewsItem.retrieved_at)
        stmt = select(NewsItem).where(ts_col >= cutoff, NewsItem.reliability >= min_reliability)
        if query:
            stmt = stmt.where(func.lower(NewsItem.title).like(f"%{query.lower()}%"))
        rows = (await self.session.scalars(stmt.order_by(desc(ts_col)).limit(limit * 3))).all()
        if category:
            rows = [r for r in rows if category in (r.categories or [])]
        return rows[:limit]

    async def news_by_cluster(self, cluster_id: str) -> Sequence[NewsItem]:
        return (
            await self.session.scalars(select(NewsItem).where(NewsItem.cluster_id == cluster_id))
        ).all()

    async def news_by_fingerprint(self, fingerprint: str) -> NewsItem | None:
        return await self.session.scalar(
            select(NewsItem).where(NewsItem.fingerprint == fingerprint)
        )

    # ---------------- strumenti IG ----------------
    async def upsert_instrument(self, epic: str, **kwargs: Any) -> Instrument:
        existing = await self.session.scalar(select(Instrument).where(Instrument.epic == epic))
        if existing:
            # il registry passa sempre il modello completo: None e' un valore voluto (es. nessun fallback)
            _apply(existing, kwargs, skip_none=False)
            return existing
        instrument = Instrument(epic=epic, **kwargs)
        self.session.add(instrument)
        await self.session.flush()
        return instrument

    async def get_instrument(self, epic: str) -> Instrument | None:
        return await self.session.scalar(select(Instrument).where(Instrument.epic == epic))

    async def list_instruments(
        self, *, asset_class: str | None = None, active_only: bool = True
    ) -> Sequence[Instrument]:
        stmt = select(Instrument)
        if asset_class:
            stmt = stmt.where(Instrument.asset_class == asset_class)
        if active_only:
            stmt = stmt.where(Instrument.active.is_(True))
        return (await self.session.scalars(stmt.order_by(Instrument.name))).all()

    async def add_instrument_price(self, **kwargs: Any) -> InstrumentPrice:
        price = InstrumentPrice(**kwargs)
        self.session.add(price)
        return price

    async def instrument_prices(
        self,
        epic: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
    ) -> Sequence[InstrumentPrice]:
        stmt = select(InstrumentPrice).where(InstrumentPrice.epic == epic)
        if since:
            stmt = stmt.where(InstrumentPrice.ts >= since)
        if until:
            stmt = stmt.where(InstrumentPrice.ts <= until)
        return (await self.session.scalars(stmt.order_by(InstrumentPrice.ts).limit(limit))).all()

    async def latest_instrument_price(self, epic: str) -> InstrumentPrice | None:
        return await self.session.scalar(
            select(InstrumentPrice)
            .where(InstrumentPrice.epic == epic)
            .order_by(desc(InstrumentPrice.ts))
            .limit(1)
        )

    async def price_at(self, epic: str, ts: datetime, *, tolerance_s: float = 120) -> InstrumentPrice | None:
        """Ultimo prezzo disponibile a/prima di `ts` (per market reaction, no lookahead)."""
        stmt = (
            select(InstrumentPrice)
            .where(
                InstrumentPrice.epic == epic,
                InstrumentPrice.ts <= ts,
                InstrumentPrice.ts >= ts - timedelta(seconds=tolerance_s),
            )
            .order_by(desc(InstrumentPrice.ts))
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def price_after(self, epic: str, ts: datetime, *, tolerance_s: float = 120) -> InstrumentPrice | None:
        """Primo prezzo disponibile a/dopo `ts`."""
        stmt = (
            select(InstrumentPrice)
            .where(
                InstrumentPrice.epic == epic,
                InstrumentPrice.ts >= ts,
                InstrumentPrice.ts <= ts + timedelta(seconds=tolerance_s),
            )
            .order_by(InstrumentPrice.ts)
            .limit(1)
        )
        return await self.session.scalar(stmt)

    # ---------------- eventi rilevati / macro ----------------
    async def upsert_detected_event(self, event_id: str, **kwargs: Any) -> DetectedEventRecord:
        existing = await self.session.scalar(
            select(DetectedEventRecord).where(DetectedEventRecord.event_id == event_id)
        )
        if existing:
            _apply(existing, kwargs)
            return existing
        record = DetectedEventRecord(event_id=event_id, **kwargs)
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_detected_event(self, event_id: str) -> DetectedEventRecord | None:
        return await self.session.scalar(
            select(DetectedEventRecord).where(DetectedEventRecord.event_id == event_id)
        )

    async def recent_detected_events(
        self, *, minutes: int = 240, status: str | None = None, limit: int = 100
    ) -> Sequence[DetectedEventRecord]:
        stmt = select(DetectedEventRecord).where(
            DetectedEventRecord.detected_at >= utcnow() - timedelta(minutes=minutes)
        )
        if status:
            stmt = stmt.where(DetectedEventRecord.status == status)
        return (
            await self.session.scalars(stmt.order_by(desc(DetectedEventRecord.detected_at)).limit(limit))
        ).all()

    async def set_event_status(self, event_id: str, status: str, **values: Any) -> None:
        await self.session.execute(
            update(DetectedEventRecord)
            .where(DetectedEventRecord.event_id == event_id)
            .values(status=status, **values)
        )

    async def upsert_macro_release(self, **kwargs: Any) -> MacroReleaseRecord:
        existing = await self.session.scalar(
            select(MacroReleaseRecord).where(
                MacroReleaseRecord.indicator == kwargs["indicator"],
                MacroReleaseRecord.country == kwargs.get("country", "US"),
                MacroReleaseRecord.release_time == kwargs["release_time"],
            )
        )
        if existing:
            _apply(existing, kwargs)
            return existing
        record = MacroReleaseRecord(**kwargs)
        self.session.add(record)
        await self.session.flush()
        return record

    async def upcoming_macro_releases(self, *, hours: int = 48) -> Sequence[MacroReleaseRecord]:
        now = utcnow()
        return (
            await self.session.scalars(
                select(MacroReleaseRecord)
                .where(
                    MacroReleaseRecord.release_time >= now - timedelta(hours=1),
                    MacroReleaseRecord.release_time <= now + timedelta(hours=hours),
                )
                .order_by(MacroReleaseRecord.release_time)
            )
        ).all()

    async def unprocessed_macro_releases(self) -> Sequence[MacroReleaseRecord]:
        return (
            await self.session.scalars(
                select(MacroReleaseRecord).where(
                    MacroReleaseRecord.processed.is_(False),
                    MacroReleaseRecord.actual.is_not(None),
                )
            )
        ).all()

    # ---------------- signals / llm ----------------
    async def add_signal(self, **kwargs: Any) -> Signal:
        signal = Signal(**kwargs)
        self.session.add(signal)
        await self.session.flush()
        return signal

    async def get_signal(self, signal_id: int) -> Signal | None:
        return await self.session.get(Signal, signal_id)

    async def open_signals(self, limit: int = 100) -> Sequence[Signal]:
        return (
            await self.session.scalars(
                select(Signal).where(Signal.status == "NEW").order_by(desc(Signal.score)).limit(limit)
            )
        ).all()

    async def recent_signals(self, *, limit: int = 50) -> Sequence[Signal]:
        return (
            await self.session.scalars(select(Signal).order_by(desc(Signal.ts)).limit(limit))
        ).all()

    async def set_signal_status(self, signal_id: int, status: str, **values: Any) -> None:
        await self.session.execute(
            update(Signal).where(Signal.id == signal_id).values(status=status, **values)
        )

    async def add_llm_decision(self, **kwargs: Any) -> LLMDecision:
        decision = LLMDecision(**kwargs)
        self.session.add(decision)
        await self.session.flush()
        return decision

    async def llm_cost_since(self, since: datetime) -> float:
        value = await self.session.scalar(
            select(func.coalesce(func.sum(LLMDecision.cost_usd), 0.0)).where(LLMDecision.ts >= since)
        )
        return float(value or 0.0)

    async def llm_calls_since(self, since: datetime) -> int:
        value = await self.session.scalar(
            select(func.count(LLMDecision.id)).where(LLMDecision.ts >= since)
        )
        return int(value or 0)

    async def recent_llm_decisions(
        self, *, limit: int = 50, agent: str | None = None, signal_id: int | None = None
    ):
        stmt = select(LLMDecision)
        if agent:
            stmt = stmt.where(LLMDecision.agent == agent)
        if signal_id is not None:
            stmt = stmt.where(LLMDecision.signal_id == signal_id)
        return (await self.session.scalars(stmt.order_by(desc(LLMDecision.ts)).limit(limit))).all()

    # ---------------- orders / fills / positions ----------------
    async def add_order(self, **kwargs: Any) -> Order:
        order = Order(**kwargs)
        self.session.add(order)
        await self.session.flush()
        return order

    async def get_order(self, client_order_id: str) -> Order | None:
        return await self.session.scalar(select(Order).where(Order.client_order_id == client_order_id))

    async def get_order_by_deal_reference(self, deal_reference: str) -> Order | None:
        return await self.session.scalar(
            select(Order).where(Order.deal_reference == deal_reference)
        )

    async def update_order(self, client_order_id: str, **values: Any) -> None:
        await self.session.execute(
            update(Order).where(Order.client_order_id == client_order_id).values(**values)
        )

    async def open_orders(self) -> Sequence[Order]:
        return (
            await self.session.scalars(
                select(Order).where(
                    Order.status.in_(
                        [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value, OrderStatus.ACCEPTED.value]
                    )
                )
            )
        ).all()

    async def recent_orders(self, *, limit: int = 50) -> Sequence[Order]:
        return (
            await self.session.scalars(select(Order).order_by(desc(Order.created_at)).limit(limit))
        ).all()

    async def orders_today(self, *, purpose: str = "OPEN") -> int:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        value = await self.session.scalar(
            select(func.count(Order.id)).where(Order.created_at >= start, Order.purpose == purpose)
        )
        return int(value or 0)

    async def rejected_orders_streak(self, lookback: int = 10) -> int:
        rows = (
            await self.session.scalars(select(Order).order_by(desc(Order.created_at)).limit(lookback))
        ).all()
        streak = 0
        for order in rows:
            if order.status == OrderStatus.REJECTED.value:
                streak += 1
            else:
                break
        return streak

    async def add_fill(self, **kwargs: Any) -> Fill:
        fill = Fill(**kwargs)
        self.session.add(fill)
        await self.session.flush()
        return fill

    async def add_position(self, **kwargs: Any) -> Position:
        position = Position(**kwargs)
        self.session.add(position)
        await self.session.flush()
        return position

    async def get_position(self, trade_id: str) -> Position | None:
        return await self.session.scalar(select(Position).where(Position.trade_id == trade_id))

    async def get_position_by_deal(self, deal_id: str) -> Position | None:
        return await self.session.scalar(select(Position).where(Position.deal_id == deal_id))

    async def update_position(self, trade_id: str, **values: Any) -> None:
        await self.session.execute(
            update(Position).where(Position.trade_id == trade_id).values(**values)
        )

    async def open_positions(self, mode: str | None = None) -> Sequence[Position]:
        stmt = select(Position).where(Position.status.in_(OPEN_POSITION_STATES))
        if mode:
            stmt = stmt.where(Position.mode == mode)
        return (await self.session.scalars(stmt)).all()

    async def positions_for_event(self, event_id: str) -> Sequence[Position]:
        return (
            await self.session.scalars(
                select(Position).where(
                    Position.event_id == event_id, Position.status.in_(OPEN_POSITION_STATES)
                )
            )
        ).all()

    async def positions_for_epic(self, epic: str) -> Sequence[Position]:
        return (
            await self.session.scalars(
                select(Position).where(Position.epic == epic, Position.status.in_(OPEN_POSITION_STATES))
            )
        ).all()

    async def closed_positions(
        self, *, since: datetime | None = None, mode: str | None = None, limit: int = 1000
    ) -> Sequence[Position]:
        stmt = select(Position).where(Position.status == PositionStatus.CLOSED.value)
        if since:
            stmt = stmt.where(Position.closed_at >= since)
        if mode:
            stmt = stmt.where(Position.mode == mode)
        return (await self.session.scalars(stmt.order_by(Position.closed_at).limit(limit))).all()

    async def realized_pnl_since(self, since: datetime, mode: str | None = None) -> float:
        stmt = select(func.coalesce(func.sum(Position.realized_pnl), 0.0)).where(
            Position.status == PositionStatus.CLOSED.value, Position.closed_at >= since
        )
        if mode:
            stmt = stmt.where(Position.mode == mode)
        return float(await self.session.scalar(stmt) or 0.0)

    # ---------------- portfolio / journal / evaluation ----------------
    async def add_portfolio_snapshot(self, **kwargs: Any) -> PortfolioSnapshot:
        snapshot = PortfolioSnapshot(**kwargs)
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def latest_portfolio(self, mode: str | None = None) -> PortfolioSnapshot | None:
        stmt = select(PortfolioSnapshot)
        if mode:
            stmt = stmt.where(PortfolioSnapshot.mode == mode)
        return await self.session.scalar(stmt.order_by(desc(PortfolioSnapshot.ts)).limit(1))

    async def portfolio_history(self, *, since: datetime | None = None, mode: str | None = None):
        stmt = select(PortfolioSnapshot)
        if since:
            stmt = stmt.where(PortfolioSnapshot.ts >= since)
        if mode:
            stmt = stmt.where(PortfolioSnapshot.mode == mode)
        return (await self.session.scalars(stmt.order_by(PortfolioSnapshot.ts))).all()

    async def add_journal_entry(self, **kwargs: Any) -> TradeJournalEntry:
        entry = TradeJournalEntry(**kwargs)
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def update_journal_entry(self, trade_id: str, **values: Any) -> None:
        await self.session.execute(
            update(TradeJournalEntry).where(TradeJournalEntry.trade_id == trade_id).values(**values)
        )

    async def get_journal_entry(self, trade_id: str) -> TradeJournalEntry | None:
        return await self.session.scalar(
            select(TradeJournalEntry).where(TradeJournalEntry.trade_id == trade_id)
        )

    async def journal_entries(
        self,
        *,
        since: datetime | None = None,
        mode: str | None = None,
        strategy_id: str | None = None,
        outcome_prefix: str | None = None,
        limit: int = 1000,
    ) -> Sequence[TradeJournalEntry]:
        stmt = select(TradeJournalEntry)
        if since:
            stmt = stmt.where(TradeJournalEntry.ts >= since)
        if mode:
            stmt = stmt.where(TradeJournalEntry.mode == mode)
        if strategy_id:
            stmt = stmt.where(TradeJournalEntry.strategy_id == strategy_id)
        if outcome_prefix:
            stmt = stmt.where(TradeJournalEntry.outcome.like(f"{outcome_prefix}%"))
        return (
            await self.session.scalars(stmt.order_by(desc(TradeJournalEntry.ts)).limit(limit))
        ).all()

    async def add_evaluation(self, **kwargs: Any) -> Evaluation:
        evaluation = Evaluation(**kwargs)
        self.session.add(evaluation)
        await self.session.flush()
        return evaluation

    async def latest_evaluation(self, kind: str, scope: str = "global") -> Evaluation | None:
        return await self.session.scalar(
            select(Evaluation)
            .where(Evaluation.kind == kind, Evaluation.scope == scope)
            .order_by(desc(Evaluation.ts))
            .limit(1)
        )

    async def evaluations(self, kind: str, *, limit: int = 100) -> Sequence[Evaluation]:
        return (
            await self.session.scalars(
                select(Evaluation).where(Evaluation.kind == kind).order_by(desc(Evaluation.ts)).limit(limit)
            )
        ).all()

    # ---------------- predictions / calibration ----------------
    async def add_prediction(self, **kwargs: Any) -> Prediction:
        prediction = Prediction(**kwargs)
        self.session.add(prediction)
        await self.session.flush()
        return prediction

    async def resolve_prediction(self, prediction_id: int, realized: int, **values: Any) -> None:
        await self.session.execute(
            update(Prediction)
            .where(Prediction.id == prediction_id)
            .values(resolved=True, realized_outcome=realized, resolved_at=utcnow(), **values)
        )

    async def unresolved_predictions(self, *, limit: int = 2000) -> Sequence[Prediction]:
        return (
            await self.session.scalars(
                select(Prediction).where(Prediction.resolved.is_(False)).order_by(Prediction.ts).limit(limit)
            )
        ).all()

    async def resolved_predictions(
        self, *, scope: str | None = None, category: str | None = None, limit: int = 5000
    ) -> Sequence[Prediction]:
        stmt = select(Prediction).where(Prediction.resolved.is_(True))
        if scope:
            stmt = stmt.where(Prediction.scope == scope)
        if category:
            stmt = stmt.where(Prediction.category == category)
        return (await self.session.scalars(stmt.order_by(Prediction.ts).limit(limit))).all()

    async def save_calibration(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            existing = await self.session.scalar(
                select(CalibrationRecord).where(
                    CalibrationRecord.scope == record["scope"],
                    CalibrationRecord.bucket == record["bucket"],
                    CalibrationRecord.as_of == record["as_of"],
                )
            )
            if existing:
                _apply(existing, record, skip_none=False)
            else:
                self.session.add(CalibrationRecord(**record))

    async def calibration_for(self, scope: str) -> Sequence[CalibrationRecord]:
        latest_as_of = await self.session.scalar(
            select(func.max(CalibrationRecord.as_of)).where(CalibrationRecord.scope == scope)
        )
        if latest_as_of is None:
            return []
        return (
            await self.session.scalars(
                select(CalibrationRecord).where(
                    CalibrationRecord.scope == scope, CalibrationRecord.as_of == latest_as_of
                )
            )
        ).all()

    # ---------------- strategie / audit / stato ----------------
    async def upsert_strategy(self, strategy_id: str, **kwargs: Any) -> Strategy:
        existing = await self.session.scalar(select(Strategy).where(Strategy.strategy_id == strategy_id))
        if existing:
            _apply(existing, kwargs)
            return existing
        strategy = Strategy(strategy_id=strategy_id, **kwargs)
        self.session.add(strategy)
        await self.session.flush()
        return strategy

    async def get_strategy(self, strategy_id: str) -> Strategy | None:
        return await self.session.scalar(select(Strategy).where(Strategy.strategy_id == strategy_id))

    async def list_strategies(self, status: str | None = None) -> Sequence[Strategy]:
        stmt = select(Strategy)
        if status:
            stmt = stmt.where(Strategy.status == status)
        return (await self.session.scalars(stmt)).all()

    async def add_audit(self, **kwargs: Any) -> AuditLog:
        entry = AuditLog(**kwargs)
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def audit_trail(self, *, limit: int = 200) -> Sequence[AuditLog]:
        return (await self.session.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(limit))).all()

    async def add_kill_switch_event(self, **kwargs: Any) -> KillSwitchEvent:
        event = KillSwitchEvent(**kwargs)
        self.session.add(event)
        await self.session.flush()
        return event

    async def active_kill_switch(self) -> KillSwitchEvent | None:
        return await self.session.scalar(
            select(KillSwitchEvent)
            .where(KillSwitchEvent.cleared_at.is_(None))
            .order_by(desc(KillSwitchEvent.ts))
            .limit(1)
        )

    async def clear_kill_switch(self, by: str) -> int:
        result = await self.session.execute(
            update(KillSwitchEvent)
            .where(KillSwitchEvent.cleared_at.is_(None))
            .values(cleared_at=utcnow(), cleared_by=by)
        )
        return int(result.rowcount or 0)

    async def set_state(self, key: str, value: str, *, by: str = "system", **details: Any) -> None:
        existing = await self.session.scalar(select(SystemStateRecord).where(SystemStateRecord.key == key))
        if existing:
            existing.value = value
            existing.details = details
            existing.updated_by = by
            existing.updated_at = utcnow()
        else:
            self.session.add(SystemStateRecord(key=key, value=value, details=details, updated_by=by))

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        record = await self.session.scalar(select(SystemStateRecord).where(SystemStateRecord.key == key))
        return record.value if record else default

    async def get_state_record(self, key: str) -> SystemStateRecord | None:
        return await self.session.scalar(select(SystemStateRecord).where(SystemStateRecord.key == key))

    # ---------------- costi / alert ----------------
    async def add_cost(self, **kwargs: Any) -> CostRecord:
        record = CostRecord(**kwargs)
        self.session.add(record)
        return record

    async def costs_since(self, since: datetime) -> dict[str, float]:
        rows = await self.session.execute(
            select(CostRecord.kind, func.sum(CostRecord.amount_usd))
            .where(CostRecord.ts >= since)
            .group_by(CostRecord.kind)
        )
        return {kind: float(total or 0.0) for kind, total in rows.all()}

    async def add_alert(self, **kwargs: Any) -> Alert:
        alert = Alert(**kwargs)
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def recent_alerts(self, *, limit: int = 50) -> Sequence[Alert]:
        return (await self.session.scalars(select(Alert).order_by(desc(Alert.ts)).limit(limit))).all()

    async def purge_prices_before(self, cutoff: datetime) -> int:
        result = await self.session.execute(delete(MarketPrice).where(MarketPrice.ts < cutoff))
        result2 = await self.session.execute(delete(InstrumentPrice).where(InstrumentPrice.ts < cutoff))
        return int((result.rowcount or 0) + (result2.rowcount or 0))
