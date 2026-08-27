"""Market collector Polymarket (sez. 4.1): batch storico, incrementale, live."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from collectors.base import BaseCollector, CollectionMode
from collectors.polymarket.client import PolymarketClient
from collectors.polymarket.parsers import (
    market_snapshot,
    parse_book,
    parse_market,
    parse_price_history,
)
from core.bus import emit
from core.clock import utcnow
from core.db import session_scope
from core.enums import EventType
from core.repository import Repository
from core.schemas import MarketSnapshot
from market.anomaly import AnomalyDetector
from market.features import book_features, combine_features, price_features


class PolymarketMarketCollector(BaseCollector):
    name = "polymarket_markets"

    def __init__(
        self,
        client: PolymarketClient | None = None,
        *,
        detector: AnomalyDetector | None = None,
        book_top_n: int = 40,
        concurrency: int = 6,
    ):
        super().__init__()
        self.client = client or PolymarketClient()
        self.detector = detector or AnomalyDetector()
        self.book_top_n = book_top_n
        self._semaphore = asyncio.Semaphore(concurrency)

    async def collect(
        self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any
    ) -> int:
        if mode is CollectionMode.HISTORICAL_BATCH:
            return await self._collect_batch(**kwargs)
        return await self._collect_incremental(**kwargs)

    # ------------------------------------------------------------------ batch
    async def _collect_batch(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 30,
        with_history: bool = True,
        history_top_n: int = 25,
        history_interval: str = "1m",
        **_: Any,
    ) -> int:
        raw_markets = await self.client.iter_markets(
            page_size=page_size, max_pages=max_pages, active=True, closed=False
        )
        stored = await self._store_markets(raw_markets)
        if with_history:
            ranked = sorted(
                raw_markets, key=lambda m: float(m.get("volumeNum") or m.get("volume") or 0), reverse=True
            )[:history_top_n]
            await asyncio.gather(
                *(self._store_history(raw, interval=history_interval) for raw in ranked)
            )
        self.stats.details["markets_seen"] = len(raw_markets)
        return stored

    async def _store_history(self, raw: dict[str, Any], *, interval: str = "1m") -> int:
        parsed = parse_market(raw)
        token_ids = [o.get("token_id") for o in parsed["outcomes"] if o.get("token_id")]
        if not token_ids:
            return 0
        outcome_names = [o["name"] for o in parsed["outcomes"] if o.get("token_id")]
        inserted = 0
        async with self._semaphore:
            for token_id, outcome in zip(token_ids, outcome_names, strict=False):
                try:
                    points = parse_price_history(
                        await self.client.price_history(str(token_id), interval=interval, fidelity=1)
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("history.failed", token=token_id, error=str(exc)[:120])
                    continue
                if not points:
                    continue
                async with session_scope() as session:
                    repo = Repository(session)
                    market = await repo.get_market("polymarket", parsed["external_id"])
                    if market is None:
                        market = await repo.upsert_market(**parsed)
                    existing = await repo.price_history(market.id, outcome=outcome, limit=1)
                    watermark = existing[0].ts if existing else None
                    for point in points:
                        if watermark and point["ts"] <= watermark:
                            continue
                        await repo.add_price(
                            market_id=market.id,
                            outcome=outcome,
                            ts=point["ts"],
                            price=point["price"],
                            source="history",
                        )
                        inserted += 1
        return inserted

    # ------------------------------------------------------------ incrementale
    async def _collect_incremental(
        self, *, limit: int = 120, with_books: bool = True, **_: Any
    ) -> int:
        raw_markets = await self.client.list_markets(
            limit=limit, active=True, closed=False, order="volume24hr"
        )
        stored = await self._store_markets(raw_markets)
        if with_books:
            ranked = sorted(
                raw_markets,
                key=lambda m: float(m.get("volume24hr") or m.get("volumeNum") or 0),
                reverse=True,
            )[: self.book_top_n]
            await asyncio.gather(*(self._collect_book(raw) for raw in ranked))
        self.stats.watermark = utcnow()
        return stored

    async def _store_markets(self, raw_markets: list[dict[str, Any]]) -> int:
        if not raw_markets:
            return 0
        count = 0
        async with session_scope() as session:
            repo = Repository(session)
            for raw in raw_markets:
                parsed = parse_market(raw)
                if not parsed["external_id"]:
                    continue
                event_slug = parsed["raw"].get("eventSlug")
                event_id = None
                if event_slug:
                    event = await repo.upsert_event(
                        slug=str(event_slug),
                        title=parsed["raw"].get("eventTitle") or parsed["question"],
                        category=parsed["category"],
                        scheduled_at=parsed["resolution_date"],
                        resolution_at=parsed["resolution_date"],
                    )
                    event_id = event.id
                market = await repo.upsert_market(event_id=event_id, **parsed)
                snapshot = market_snapshot(raw)
                if snapshot.price is not None:
                    await repo.add_price(
                        market_id=market.id,
                        outcome=snapshot.outcome,
                        ts=snapshot.ts,
                        price=snapshot.price,
                        best_bid=snapshot.best_bid,
                        best_ask=snapshot.best_ask,
                        spread=snapshot.spread,
                        mid=snapshot.mid,
                        volume=snapshot.volume,
                        liquidity=snapshot.liquidity,
                        source="gamma",
                    )
                if market.status == "CLOSED" and market.resolved_outcome:
                    await emit(
                        EventType.MARKET_RESOLVED,
                        {
                            "venue": "polymarket",
                            "market_id": market.external_id,
                            "outcome": market.resolved_outcome,
                        },
                        source=self.name,
                    )
                count += 1
        return count

    async def _collect_book(self, raw: dict[str, Any]) -> None:
        parsed = parse_market(raw)
        outcomes = [o for o in parsed["outcomes"] if o.get("token_id")]
        if not outcomes:
            return
        async with self._semaphore:
            try:
                books = await self.client.get_books([str(o["token_id"]) for o in outcomes])
            except Exception as exc:  # noqa: BLE001
                self.log.warning("book.failed", market=parsed["external_id"], error=str(exc)[:120])
                return
        async with session_scope() as session:
            repo = Repository(session)
            market = await repo.get_market("polymarket", parsed["external_id"])
            if market is None:
                market = await repo.upsert_market(**parsed)
            for outcome, raw_book in zip(outcomes, books, strict=False):
                book = parse_book(raw_book, market_id=parsed["external_id"], outcome=outcome["name"])
                features = book_features(book)
                await repo.add_orderbook(
                    market_id=market.id,
                    outcome=outcome["name"],
                    ts=book.ts,
                    bids=[level.model_dump() for level in book.bids],
                    asks=[level.model_dump() for level in book.asks],
                    features=features.as_dict(),
                    status=book.status,
                )
                if book.mid is not None:
                    await repo.add_price(
                        market_id=market.id,
                        outcome=outcome["name"],
                        ts=book.ts,
                        price=book.mid,
                        best_bid=book.best_bid,
                        best_ask=book.best_ask,
                        spread=book.spread,
                        mid=book.mid,
                        source="clob",
                    )
                    await emit(
                        EventType.PRICE_CHANGED,
                        {
                            "venue": "polymarket",
                            "market_id": parsed["external_id"],
                            "outcome": outcome["name"],
                            "price": book.mid,
                            "best_bid": book.best_bid,
                            "best_ask": book.best_ask,
                        },
                        source=self.name,
                    )
                await self._check_anomalies(repo, market, outcome["name"], features.as_dict())

    async def _check_anomalies(
        self, repo: Repository, market: Any, outcome: str, book_feature_dict: dict[str, float]
    ) -> None:
        history = await repo.price_history(
            market.id, outcome=outcome, since=utcnow() - timedelta(hours=6), limit=400
        )
        points = [(row.ts, row.price) for row in history]
        volumes = [(row.ts, row.volume) for row in history if row.volume is not None]
        liquidity = [(row.ts, row.liquidity) for row in history if row.liquidity is not None]
        anomalies = []
        price_anomaly = self.detector.detect_price_anomaly(
            market.external_id,
            points,
            volumes=volumes,
            scheduled_at=market.resolution_date,
            market_db_id=market.id,
        )
        if price_anomaly:
            anomalies.append(price_anomaly)
        liquidity_anomaly = self.detector.detect_liquidity_anomaly(
            market.external_id, liquidity, market_db_id=market.id
        )
        if liquidity_anomaly:
            anomalies.append(liquidity_anomaly)
        imbalance_anomaly = self.detector.detect_book_imbalance(
            market.external_id,
            book_feature_dict.get("order_book_imbalance"),
            market_db_id=market.id,
            depth=book_feature_dict.get("depth_3"),
        )
        if imbalance_anomaly:
            anomalies.append(imbalance_anomaly)

        for anomaly in anomalies:
            await repo.add_anomaly(
                market_id=market.id,
                ts=anomaly.ts,
                kind=anomaly.kind,
                severity=anomaly.severity,
                details=anomaly.details,
            )
            await emit(EventType.ANOMALY_DETECTED, anomaly.as_dict(), source=self.name)

    # ------------------------------------------------------------------ helpers
    async def snapshot_for(self, market_external_id: str) -> MarketSnapshot | None:
        """Snapshot live di un singolo mercato (usato dai tool degli agenti)."""
        raw = await self.client.get_market(market_external_id)
        if raw is None:
            return None
        snapshot = market_snapshot(raw)
        token_id = snapshot.raw.get("token_id")
        if token_id:
            try:
                book = parse_book(
                    await self.client.get_book(str(token_id)),
                    market_id=snapshot.market_id,
                    outcome=snapshot.outcome,
                )
                snapshot.book = book
                snapshot.best_bid = book.best_bid
                snapshot.best_ask = book.best_ask
                snapshot.spread = book.spread
                snapshot.mid = book.mid
                snapshot.features = combine_features(
                    book_features(book).as_dict(),
                    price_features([(snapshot.ts, snapshot.price or 0.0)]).as_dict(),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning("snapshot.book_failed", market=market_external_id, error=str(exc)[:120])
        return snapshot

    async def aclose(self) -> None:
        await self.client.aclose()


async def historical_backfill(
    client: PolymarketClient | None = None, *, pages: int = 30, history_top_n: int = 50
) -> dict[str, int]:
    """Entry point per il backfill iniziale (script/CLI)."""
    collector = PolymarketMarketCollector(client)
    markets = await collector.run_once(
        CollectionMode.HISTORICAL_BATCH,
        max_pages=pages,
        history_top_n=history_top_n,
        history_interval="1h",
    )
    return {"markets": markets}
