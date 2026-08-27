"""Wallet collector Polymarket (sez. 5.1): discovery e raccolta storico.

Target dichiarato nei requisiti: 10.000-20.000 wallet. La discovery combina piu
canali indipendenti perche nessuno da solo copre l'universo:
  1. holders dei mercati piu liquidi;
  2. leaderboard pubblica (quando esposta);
  3. controparti dei trade recenti sui mercati monitorati;
  4. wallet gia noti a DB, per aggiornamento incrementale.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, func, select

from collectors.base import BaseCollector, CollectionMode
from collectors.polymarket.client import PolymarketClient
from collectors.polymarket.parsers import (
    parse_holder,
    parse_leaderboard_entry,
    parse_wallet_position,
    parse_wallet_trade,
)
from core.bus import emit
from core.clock import utcnow
from core.db import session_scope
from core.enums import EventType
from core.models import Market, Wallet, WalletTrade
from core.repository import Repository


class PolymarketWalletCollector(BaseCollector):
    name = "polymarket_wallets"

    def __init__(
        self,
        client: PolymarketClient | None = None,
        *,
        concurrency: int = 8,
        trades_page_size: int = 500,
        max_trade_pages: int = 6,
    ):
        super().__init__()
        self.client = client or PolymarketClient()
        self.trades_page_size = trades_page_size
        self.max_trade_pages = max_trade_pages
        self._semaphore = asyncio.Semaphore(concurrency)

    async def collect(
        self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any
    ) -> int:
        if mode is CollectionMode.HISTORICAL_BATCH:
            return await self._collect_batch(**kwargs)
        return await self._collect_incremental(**kwargs)

    # ------------------------------------------------------------- discovery
    async def discover(
        self,
        *,
        markets_limit: int = 60,
        holders_per_market: int = 100,
        use_leaderboard: bool = True,
        target: int = 20_000,
    ) -> list[str]:
        """Restituisce indirizzi candidati, senza scaricarne lo storico."""
        found: dict[str, dict[str, Any]] = {}

        if use_leaderboard:
            for window in ("all", "30d", "7d"):
                for kind in ("pnl", "volume"):
                    for raw in await self.client.leaderboard(window=window, kind=kind, limit=200):
                        entry = parse_leaderboard_entry(raw)
                        if entry["address"]:
                            found.setdefault(entry["address"], {}).update(
                                {"label": entry["label"], "source": f"leaderboard:{window}:{kind}"}
                            )

        async with session_scope() as session:
            markets = (
                await session.scalars(
                    select(Market)
                    .where(Market.venue == "polymarket", Market.status == "OPEN")
                    .order_by(desc(Market.volume))
                    .limit(markets_limit)
                )
            ).all()
            condition_ids = [m.external_id for m in markets]

        async def holders_for(condition_id: str) -> None:
            async with self._semaphore:
                try:
                    raw_holders = await self.client.market_holders(
                        condition_id, limit=holders_per_market
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning(
                        "discovery.holders_failed", market=condition_id, error=str(exc)[:120]
                    )
                    return
            for raw in raw_holders:
                holder = parse_holder(raw)
                if holder["address"]:
                    found.setdefault(holder["address"], {}).update(
                        {"label": holder["label"], "source": f"holders:{condition_id}"}
                    )

        await asyncio.gather(*(holders_for(cid) for cid in condition_ids))

        # controparti dei trade recenti sui mercati monitorati
        async def traders_for(condition_id: str) -> None:
            async with self._semaphore:
                try:
                    trades = await self.client.market_trades(condition_id, limit=200)
                except Exception:  # noqa: BLE001
                    return
            for raw in trades:
                parsed = parse_wallet_trade(raw)
                if parsed["wallet_address"]:
                    found.setdefault(parsed["wallet_address"], {}).update(
                        {"source": f"market_trades:{condition_id}"}
                    )

        await asyncio.gather(*(traders_for(cid) for cid in condition_ids[:20]))

        addresses = [a for a in found if a and a.startswith("0x")][:target]
        async with session_scope() as session:
            repo = Repository(session)
            for address in addresses:
                info = found.get(address, {})
                await repo.upsert_wallet(
                    address=address,
                    label=info.get("label"),
                    flags={"discovery_source": info.get("source")},
                )
        self.stats.details["discovered"] = len(addresses)
        self.log.info("discovery.complete", wallets=len(addresses))
        return addresses

    # --------------------------------------------------------------- raccolta
    async def collect_wallet(self, address: str, *, full_history: bool = False) -> int:
        """Scarica trade e posizioni di un wallet. Restituisce i trade nuovi."""
        pages = self.max_trade_pages if full_history else 1
        all_trades: list[dict[str, Any]] = []
        async with self._semaphore:
            for page in range(pages):
                try:
                    raw_trades = await self.client.wallet_trades(
                        address, limit=self.trades_page_size, offset=page * self.trades_page_size
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("wallet.trades_failed", address=address, error=str(exc)[:120])
                    break
                if not raw_trades:
                    break
                all_trades.extend(parse_wallet_trade(raw, address=address) for raw in raw_trades)
                if len(raw_trades) < self.trades_page_size:
                    break
            try:
                raw_positions = await self.client.wallet_positions(address)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("wallet.positions_failed", address=address, error=str(exc)[:120])
                raw_positions = []

        inserted = 0
        async with session_scope() as session:
            repo = Repository(session)
            if all_trades:
                inserted = await repo.add_wallet_trades(all_trades)
            unrealized = 0.0
            realized = 0.0
            for raw in raw_positions:
                position = parse_wallet_position(raw, address=address)
                if not position["asset_id"]:
                    continue
                unrealized += position.get("unrealized_pnl") or 0.0
                realized += position.get("realized_pnl") or 0.0
                await repo.upsert_wallet_position(
                    address,
                    position.pop("asset_id"),
                    **{k: v for k, v in position.items() if k != "wallet_address"},
                )
            timestamps = [t["ts"] for t in all_trades if t.get("ts")]
            await repo.upsert_wallet(
                address=address,
                last_seen=max(timestamps) if timestamps else utcnow(),
                first_seen=min(timestamps) if timestamps else None,
                unrealized_pnl=unrealized,
                realized_pnl=realized or None,
            )
        if inserted:
            for trade in all_trades[-inserted:]:
                await emit(
                    EventType.WALLET_TRADE,
                    {
                        "wallet": address,
                        "market_id": trade.get("condition_id"),
                        "category": trade.get("category"),
                        "side": trade.get("side"),
                        "outcome": trade.get("outcome"),
                        "price": trade.get("price"),
                        "usd_size": trade.get("usd_size"),
                        "ts": trade["ts"].isoformat() if trade.get("ts") else None,
                    },
                    source=self.name,
                )
        return inserted

    async def _collect_batch(
        self,
        *,
        target: int = 20_000,
        markets_limit: int = 60,
        holders_per_market: int = 100,
        addresses: list[str] | None = None,
        **_: Any,
    ) -> int:
        candidates = addresses or await self.discover(
            markets_limit=markets_limit, holders_per_market=holders_per_market, target=target
        )
        results = await asyncio.gather(
            *(self.collect_wallet(address, full_history=True) for address in candidates),
            return_exceptions=True,
        )
        total = 0
        for result in results:
            if isinstance(result, int):
                total += result
            else:
                self.stats.errors += 1
        self.stats.details["wallets_processed"] = len(candidates)
        return total

    async def _collect_incremental(
        self, *, batch_size: int = 200, stale_minutes: int = 60, **_: Any
    ) -> int:
        """Aggiorna i wallet piu rilevanti/non aggiornati di recente."""
        cutoff = utcnow() - timedelta(minutes=stale_minutes)
        async with session_scope() as session:
            rows = (
                await session.scalars(
                    select(Wallet)
                    .where((Wallet.updated_at < cutoff) | (Wallet.n_trades == 0))
                    .order_by(desc(Wallet.total_volume))
                    .limit(batch_size)
                )
            ).all()
            addresses = [w.address for w in rows]
        if not addresses:
            return 0
        results = await asyncio.gather(
            *(self.collect_wallet(address) for address in addresses), return_exceptions=True
        )
        total = sum(r for r in results if isinstance(r, int))
        self.stats.details["wallets_processed"] = len(addresses)
        self.stats.watermark = utcnow()
        return total

    async def refresh_trade_counters(self) -> int:
        """Ricalcola i contatori aggregati sui wallet (n_trades, volume, n_markets)."""
        async with session_scope() as session:
            rows = await session.execute(
                select(
                    WalletTrade.wallet_address,
                    func.count(WalletTrade.id),
                    func.coalesce(func.sum(WalletTrade.usd_size), 0.0),
                    func.count(func.distinct(WalletTrade.condition_id)),
                    func.min(WalletTrade.ts),
                    func.max(WalletTrade.ts),
                ).group_by(WalletTrade.wallet_address)
            )
            repo = Repository(session)
            updated = 0
            for address, n_trades, volume, n_markets, first_ts, last_ts in rows.all():
                await repo.upsert_wallet(
                    address=address,
                    n_trades=int(n_trades or 0),
                    total_volume=float(volume or 0.0),
                    n_markets=int(n_markets or 0),
                    first_seen=first_ts,
                    last_seen=last_ts,
                )
                updated += 1
        return updated

    async def aclose(self) -> None:
        await self.client.aclose()
