"""IG price collector: polling quote (REST o fallback) per l'universo e persistenza serie.

Necessario per market reaction (patch sez. 7) e post-signal alpha (sez. 35):
senza una serie storica fine, il "quanto e' gia prezzato" non e' misurabile.
"""
from __future__ import annotations

from typing import Any

from collectors.base import BaseCollector, CollectionMode
from core.clock import utcnow
from market.instrument_registry import InstrumentRegistry, get_registry
from market.prices import PriceService, get_price_service


class IGPriceCollector(BaseCollector):
    name = "ig_prices"

    def __init__(self, *, prices: PriceService | None = None, registry: InstrumentRegistry | None = None, epics: list[str] | None = None, stream_getter: Any | None = None, rest_cap_per_cycle: int = 6):
        super().__init__()
        self.prices = prices or get_price_service()
        self.registry = registry or get_registry()
        self.epics = epics
        # quando lo streaming e' sano i prezzi arrivano da li: il REST serve solo per gli
        # epic senza quote fresca, con un tetto per ciclo per rispettare l'allowance IG.
        self._stream_getter = stream_getter
        self.rest_cap_per_cycle = rest_cap_per_cycle
        self._persist_every = 0

    @property
    def _stream_healthy(self) -> bool:
        stream = self._stream_getter() if self._stream_getter else None
        return bool(stream and stream.healthy)

    def _target_epics(self) -> list[str]:
        if self.epics:
            return self.epics
        instruments = self.registry.all()
        # con IG collegato interroga solo gli epic validati (raw.market_id): evita 403 ripetuti
        # su epic inesistenti; senza IG usa quelli con simbolo pubblico di fallback.
        if any((i.raw or {}).get("market_id") for i in instruments):
            return [i.epic for i in instruments if (i.raw or {}).get("market_id")]
        return [i.epic for i in instruments if i.fallback_symbol]

    async def collect(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        epics = self._target_epics()
        if mode is CollectionMode.HISTORICAL_BATCH:
            total = 0
            for epic in epics:
                instrument = self.registry.get(epic)
                try:
                    candles = await self.prices.candles(epic, minutes=kwargs.get("minutes", 24 * 60))
                except Exception as exc:  # noqa: BLE001 - allowance/epic mancante non deve fermare il backfill
                    self.log.info("ig_prices.history_skip", epic=epic, error=str(exc)[:100])
                    continue
                total += await self.prices.persist_candles(candles, spread=(instrument.spread or 0.0) if instrument else 0.0)
            return total

        max_age = self.prices.max_staleness_s
        stream_ok = self._stream_healthy
        # gli epic gia coperti da una quote fresca (di norma dallo stream) non si toccano via REST
        stale = [e for e in epics if (q := self.prices.cached(e)) is None or q.age_seconds() > max_age]
        if stream_ok:
            stale = stale[: self.rest_cap_per_cycle]  # allowance guard: pochi epic per ciclo
        quotes = await self.prices.quotes(stale, max_age_s=max_age, persist=True) if stale else {}
        live = {e: self.prices.cached(e) for e in epics if self.prices.cached(e)}
        self.stats.details["source"] = self.prices.live_source
        self.stats.details["stream"] = stream_ok
        self.stats.details["rest_fetched"] = len(quotes)
        self.stats.details["fresh_epics"] = sum(1 for q in live.values() if q and q.age_seconds() <= max_age)
        self.stats.details["tradeable"] = sum(1 for q in live.values() if q and q.market_status.tradeable)
        self.stats.watermark = utcnow()
        return len(live)
