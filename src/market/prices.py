"""Servizio prezzi (patch sez. 3, 21, 25, 41).

Ordine di preferenza: streaming IG -> REST IG -> fonte pubblica di fallback.
Iron rule "nessun prezzo inventato": ogni Quote porta `source` e `ts`; il
fallback pubblico e' dichiarato e non e' mai usato per ordini reali su IG.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.clock import utcnow
from core.config import get_settings
from core.db import session_scope
from core.enums import MarketStatus
from core.errors import StaleDataError, UpstreamError
from core.logging import get_logger
from core.repository import Repository
from core.schemas import Candle, Instrument, Quote
from market.instrument_registry import InstrumentRegistry, get_registry

log = get_logger("market.prices")

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class PublicPriceProvider:
    """Fallback pubblico (Yahoo Finance chart API): mid reale, spread tipico IG.

    Usato quando IG non e' collegato (test/paper) o come cross-check.
    """

    name = "yahoo"

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (ATS research bot)"}
        )
        self._own = http is None
        self._sem = asyncio.Semaphore(4)

    async def quote(self, instrument: Instrument) -> Quote | None:
        if not instrument.fallback_symbol:
            return None
        data = await self._chart(instrument.fallback_symbol, interval="1m", range_="1d")
        if data is None:
            return None
        meta = data.get("meta") or {}
        price = meta.get("regularMarketPrice")
        timestamps = data.get("timestamp") or []
        closes = ((data.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        last_ts = None
        for ts, close in zip(reversed(timestamps), reversed(closes), strict=False):
            if close is not None:
                last_ts = ts
                if price is None:
                    price = close
                break
        if price is None:
            return None
        scale = float((instrument.raw or {}).get("fallback_scale") or 1.0)
        price = float(price) * scale
        ts = datetime.fromtimestamp(int(meta.get("regularMarketTime") or last_ts or 0), tz=UTC) if (meta.get("regularMarketTime") or last_ts) else utcnow()
        half_spread = (instrument.spread or 0.0) / 2.0
        state = str(meta.get("marketState") or "").upper()
        # REGULAR/PRE/POST = sottostante che prezza; PREPRE/POSTPOST/CLOSED = fermo (il prezzo sarebbe stale)
        status = MarketStatus.TRADEABLE if state in ("", "REGULAR", "PRE", "POST") else MarketStatus.CLOSED
        return Quote(
            epic=instrument.epic,
            bid=float(price) - half_spread,
            offer=float(price) + half_spread,
            ts=ts,
            market_status=status,
            source=self.name,
            change_pct=(float(meta["regularMarketPrice"]) / float(meta["chartPreviousClose"]) - 1)
            if meta.get("regularMarketPrice") and meta.get("chartPreviousClose")
            else None,
            raw={"symbol": instrument.fallback_symbol, "market_state": state},
        )

    async def candles(
        self, instrument: Instrument, *, interval: str = "1m", range_: str = "1d"
    ) -> list[Candle]:
        if not instrument.fallback_symbol:
            return []
        data = await self._chart(instrument.fallback_symbol, interval=interval, range_=range_)
        if data is None:
            return []
        timestamps = data.get("timestamp") or []
        quote = ((data.get("indicators") or {}).get("quote") or [{}])[0]
        out: list[Candle] = []
        scale = float((instrument.raw or {}).get("fallback_scale") or 1.0)
        for index, ts in enumerate(timestamps):
            close = _at(quote.get("close"), index)
            if close is None:
                continue
            close *= scale
            out.append(
                Candle(
                    epic=instrument.epic,
                    ts=datetime.fromtimestamp(int(ts), tz=UTC),
                    open=(_at(quote.get("open"), index) or close / scale) * scale,
                    high=(_at(quote.get("high"), index) or close / scale) * scale,
                    low=(_at(quote.get("low"), index) or close / scale) * scale,
                    close=close,
                    volume=_at(quote.get("volume"), index),
                    source=self.name,
                )
            )
        return out

    async def _chart(self, symbol: str, *, interval: str, range_: str) -> dict[str, Any] | None:
        async with self._sem:
            try:
                response = await self._http.get(
                    YAHOO_CHART.format(symbol=symbol), params={"interval": interval, "range": range_}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log.warning("yahoo.unavailable", symbol=symbol, error=str(exc)[:100])
                return None
        if response.status_code >= 400:
            log.warning("yahoo.http_error", symbol=symbol, status=response.status_code)
            return None
        try:
            result = response.json()["chart"]["result"]
        except (KeyError, ValueError, TypeError):
            return None
        return result[0] if result else None

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()


class IGPriceProvider:
    """Prezzi da IG REST (snapshot e storico)."""

    name = "ig-rest"

    def __init__(self, client: Any):
        self.client = client

    async def quote(self, instrument: Instrument) -> Quote | None:
        details = await self.client.get_market_details(instrument.epic)
        snapshot = details.get("snapshot") or {}
        bid, offer = snapshot.get("bid"), snapshot.get("offer")
        if bid is None or offer is None:
            return None
        scaling = float(snapshot.get("scalingFactor") or 1.0)
        ts = _parse_ig_time(snapshot.get("updateTimeUTC") or snapshot.get("updateTime"))
        return Quote(
            epic=instrument.epic,
            bid=float(bid),
            offer=float(offer),
            ts=ts or utcnow(),
            market_status=MarketStatus.parse(snapshot.get("marketStatus")),
            source=self.name,
            high=_f(snapshot.get("high")),
            low=_f(snapshot.get("low")),
            change_pct=(_f(snapshot.get("percentageChange")) or 0.0) / 100.0,
            delay_ms=int(snapshot.get("delayTime") or 0) * 1000,
            raw={"scaling_factor": scaling, "details": {"dealingRules": details.get("dealingRules")}},
        )

    async def candles(
        self, instrument: Instrument, *, resolution: str = "MINUTE", max_points: int = 200,
        from_ts: datetime | None = None, to_ts: datetime | None = None,
    ) -> list[Candle]:
        data = await self.client.get_historical_prices(
            instrument.epic, resolution=resolution, max_points=max_points, from_ts=from_ts, to_ts=to_ts
        )
        out: list[Candle] = []
        for point in data.get("prices") or []:
            ts = _parse_ig_time(point.get("snapshotTimeUTC") or point.get("snapshotTime"))
            if ts is None:
                continue
            out.append(
                Candle(
                    epic=instrument.epic,
                    ts=ts,
                    open=_mid_of(point.get("openPrice")),
                    high=_mid_of(point.get("highPrice")),
                    low=_mid_of(point.get("lowPrice")),
                    close=_mid_of(point.get("closePrice")),
                    volume=_f(point.get("lastTradedVolume")),
                    source=self.name,
                )
            )
        return out


class PriceService:
    """Facade: cache live (stream), REST IG, fallback pubblico; persistenza serie."""

    def __init__(
        self,
        *,
        registry: InstrumentRegistry | None = None,
        ig_provider: IGPriceProvider | None = None,
        public_provider: PublicPriceProvider | None = None,
        max_staleness_s: float | None = None,
        allow_public_fallback: bool = True,
    ):
        settings = get_settings()
        self.registry = registry or get_registry()
        self.ig_provider = ig_provider
        self.public_provider = public_provider or PublicPriceProvider()
        self.max_staleness_s = max_staleness_s or settings.risk.max_data_staleness_s
        self.allow_public_fallback = allow_public_fallback
        self._live: dict[str, Quote] = {}
        self._history: dict[str, list[Quote]] = {}
        self.max_history = 5000

    # ------------------------------------------------------------ live cache
    def push_live(self, quote: Quote) -> None:
        """Chiamato dallo streaming (patch sez. 25)."""
        self._live[quote.epic] = quote
        self._remember(quote)
        self.registry.update_status(quote.epic, quote.market_status)

    def _remember(self, quote: Quote) -> None:
        bucket = self._history.setdefault(quote.epic, [])
        bucket.append(quote)
        if len(bucket) > self.max_history:
            del bucket[: -self.max_history]

    def cached(self, epic: str) -> Quote | None:
        return self._live.get(epic)

    def recent(self, epic: str, *, seconds: float) -> list[Quote]:
        cutoff = utcnow() - timedelta(seconds=seconds)
        return [q for q in self._history.get(epic, []) if q.ts >= cutoff]

    # ------------------------------------------------------------- get quote
    async def quote(self, epic: str, *, max_age_s: float | None = None, persist: bool = True) -> Quote:
        """Quote fresca: stream -> REST IG -> fallback pubblico. Solleva StaleDataError."""
        instrument = self.registry.get(epic)
        if instrument is None:
            raise UpstreamError(f"epic sconosciuto nel registry: {epic}", provider="registry")
        limit = max_age_s if max_age_s is not None else self.max_staleness_s

        live = self._live.get(epic)
        if live is not None and live.age_seconds() <= limit:
            return live

        quote: Quote | None = None
        errors: list[str] = []
        if self.ig_provider is not None:
            try:
                quote = await self.ig_provider.quote(instrument)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ig: {str(exc)[:120]}")
        if quote is None and self.allow_public_fallback:
            try:
                quote = await self.public_provider.quote(instrument)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"public: {str(exc)[:120]}")
        if quote is None:
            if self.ig_provider is None and not instrument.fallback_symbol:
                raise UpstreamError(f"{epic}: nessuna fonte prezzi senza IG (fallback pubblico assente)", provider="prices")
            raise UpstreamError(f"nessun prezzo disponibile per {epic}: {errors}", provider="prices")

        self._live[epic] = quote
        self._remember(quote)
        self.registry.update_status(epic, quote.market_status, spread=quote.spread)
        if persist:
            await self._persist(quote)
        return quote

    async def quotes(self, epics: list[str], **kwargs: Any) -> dict[str, Quote]:
        results = await asyncio.gather(*(self.quote(e, **kwargs) for e in epics), return_exceptions=True)
        out: dict[str, Quote] = {}
        for epic, result in zip(epics, results, strict=False):
            if isinstance(result, Quote):
                out[epic] = result
            elif "fallback pubblico assente" in str(result):
                log.debug("prices.quote_unavailable", epic=epic)
            else:
                log.warning("prices.quote_failed", epic=epic, error=str(result)[:120])
        return out

    def staleness_limit(self, quote: Quote) -> float:
        """Soglia di staleness: stretta per feed broker, piu larga per dati pubblici a 1 minuto (solo PAPER/SHADOW)."""
        if quote.source.startswith("ig"):
            return self.max_staleness_s
        return get_settings().risk.max_public_data_staleness_s

    def require_fresh(self, quote: Quote, *, max_age_s: float | None = None) -> Quote:
        """Iron rule: nessun trade con feed stale."""
        limit = max_age_s if max_age_s is not None else self.staleness_limit(quote)
        age = quote.age_seconds()
        if age > limit:
            raise StaleDataError(f"quote {quote.epic} stale: {age:.1f}s > {limit}s (source={quote.source})")
        return quote

    # --------------------------------------------------------------- storico
    async def candles(self, epic: str, *, minutes: int = 240, resolution: str = "MINUTE") -> list[Candle]:
        instrument = self.registry.get(epic)
        if instrument is None:
            return []
        if self.ig_provider is not None:
            try:
                return await self.ig_provider.candles(
                    instrument, resolution=resolution, max_points=min(minutes, 500)
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("prices.ig_candles_failed", epic=epic, error=str(exc)[:120])
        if self.allow_public_fallback:
            range_ = "1d" if minutes <= 24 * 60 else "5d"
            candles = await self.public_provider.candles(instrument, interval="1m", range_=range_)
            cutoff = utcnow() - timedelta(minutes=minutes)
            return [c for c in candles if c.ts >= cutoff]
        return []

    async def price_series(self, epic: str, *, since: datetime, until: datetime | None = None) -> list[tuple[datetime, float]]:
        """Serie mid da DB (persistita dal collector) - usata per market reaction/post-signal alpha."""
        async with session_scope() as session:
            rows = await Repository(session).instrument_prices(epic, since=since, until=until)
        series = [(row.ts, row.mid) for row in rows]
        for quote in self._history.get(epic, []):
            if quote.ts >= since and (until is None or quote.ts <= until):
                series.append((quote.ts, quote.mid))
        series.sort(key=lambda p: p[0])
        return series

    async def _persist(self, quote: Quote) -> None:
        try:
            async with session_scope() as session:
                await Repository(session).add_instrument_price(
                    epic=quote.epic,
                    ts=quote.ts,
                    bid=quote.bid,
                    offer=quote.offer,
                    mid=quote.mid,
                    market_status=quote.market_status.value,
                    source=quote.source,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("prices.persist_failed", epic=quote.epic, error=str(exc)[:120])

    async def persist_candles(self, candles: list[Candle], *, spread: float = 0.0) -> int:
        if not candles:
            return 0
        async with session_scope() as session:
            repo = Repository(session)
            existing = await repo.instrument_prices(
                candles[0].epic, since=candles[0].ts - timedelta(seconds=1), until=candles[-1].ts + timedelta(seconds=1), limit=20000
            )
            known = {(row.ts, row.source) for row in existing}
            inserted = 0
            for candle in candles:
                if (candle.ts, candle.source) in known:
                    continue
                await repo.add_instrument_price(
                    epic=candle.epic,
                    ts=candle.ts,
                    bid=candle.close - spread / 2,
                    offer=candle.close + spread / 2,
                    mid=candle.close,
                    market_status="UNKNOWN",
                    source=candle.source,
                    volume=candle.volume,
                )
                inserted += 1
        return inserted

    @property
    def live_source(self) -> str:
        if self._live:
            return next(iter(self._live.values())).source
        return "ig-rest" if self.ig_provider else "yahoo"

    async def aclose(self) -> None:
        await self.public_provider.aclose()


def _at(values: list[Any] | None, index: int) -> float | None:
    if not values or index >= len(values):
        return None
    value = values[index]
    return float(value) if value is not None else None


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mid_of(point: dict[str, Any] | None) -> float:
    if not point:
        return 0.0
    bid, ask = _f(point.get("bid")), _f(point.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid or ask or _f(point.get("lastTraded")) or 0.0


def _parse_ig_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M:%S":
                now = utcnow()
                parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_service: PriceService | None = None


def get_price_service() -> PriceService:
    global _service
    if _service is None:
        _service = PriceService()
    return _service


def set_price_service(service: PriceService | None) -> None:
    global _service
    _service = service
