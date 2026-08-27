"""Collector mercati Limitless: scansiona TUTTO il disponibile, quota, seleziona candidati.

Ogni ciclo: pagina l'intero universo attivo, persiste i mercati (venue="limitless"),
crea/aggiorna pseudo-strumenti `LMTS:<id>` nel registry, pubblica Quote YES nel
PriceService (bid/offer sintetici dallo spread tipico) e ordina i candidati per il
decision loop. Nessuna chiave richiesta: endpoint pubblici.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from collectors.base import BaseCollector, CollectionMode
from core.clock import utcnow
from core.db import session_scope
from core.enums import AssetClass, MarketStatus
from core.schemas import Instrument, Quote
from core.repository import Repository
from execution.limitless.client import NOISE_TAGS, LimitlessClient, parse_market

EPIC_PREFIX = "LMTS:"
HALF_SPREAD = 0.01  # meta' spread sintetico quando il book non e' disponibile


def market_epic(market_id: str) -> str:
    return f"{EPIC_PREFIX}{market_id}"


def parse_expiry(raw: dict[str, Any]) -> datetime | None:
    ts = raw.get("expirationTimestamp")
    if ts:
        try:
            value = float(ts)
            return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    text = raw.get("expirationDate") or raw.get("deadline")
    if isinstance(text, str):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class LimitlessCandidate:
    market_id: str
    epic: str
    title: str
    slug: str | None
    yes_price: float
    volume: float
    expiry: datetime | None
    categories: list[str]
    priority: float
    tokens: dict | None = None
    trade_type: str = "amm"


def eligible(parsed: dict[str, Any], expiry: datetime | None, cfg: Any, now: datetime) -> bool:
    """Giudicabile in linea di principio: prezzo informativo, no scalping da rumore."""
    yes = parsed.get("yes_price")
    if yes is None or not (cfg.min_price <= yes <= cfg.max_price):
        return False
    if str(parsed.get("collateral") or "").upper() not in ("USDC", "USD", ""):
        return False
    tags = {str(t).lower() for t in (parsed.get("tags") or [])} | {str(c).lower() for c in (parsed.get("categories") or [])}
    if tags & NOISE_TAGS:
        return False
    # direzione intraday di prezzi (BTC/azioni 'up or down'): monetina per costruzione,
    # nessun edge di giudizio -> fuori PRIMA del comitato (slot LLM preziosi)
    if 'up or down' in str(parsed.get('title') or '').lower():
        return False
    if expiry is not None:
        hours = (expiry - now).total_seconds() / 3600
        if hours < cfg.min_hours_to_expiry:
            return False
    return True


def priority_of(parsed: dict[str, Any], expiry: datetime | None, now: datetime) -> float:
    """Volume pesato per orizzonte: il comitato ha edge su orizzonti >6h, non sul rumore intraday."""
    import math

    volume = float(parsed.get("volume") or 0.0)
    score = math.log10(volume + 10.0)
    if expiry is not None:
        hours = (expiry - now).total_seconds() / 3600
        if hours < 2 or hours > 24 * 45:
            score *= 0.4
        elif hours <= 24:
            score *= 1.6  # risolve oggi: l'edge si incassa in giornata
    return score


def select_candidates(parsed_markets: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, now: datetime) -> list[LimitlessCandidate]:
    out: list[LimitlessCandidate] = []
    for parsed, raw in parsed_markets:
        expiry = parse_expiry(raw)
        if not eligible(parsed, expiry, cfg, now):
            continue
        out.append(
            LimitlessCandidate(
                market_id=parsed["id"],
                epic=market_epic(parsed["id"]),
                title=str(parsed.get("title") or "")[:300],
                slug=raw.get("slug"),
                yes_price=float(parsed["yes_price"]),
                volume=float(parsed.get("volume") or 0.0),
                expiry=expiry,
                categories=[str(c) for c in (parsed.get("categories") or [])][:6],
                priority=priority_of(parsed, expiry, now),
                tokens=parsed.get("tokens") if isinstance(parsed.get("tokens"), dict) else None,
                trade_type=str(raw.get("tradeType") or ("clob" if parsed.get("tokens") else "amm")).lower(),
            )
        )
    out.sort(key=lambda c: c.priority, reverse=True)
    return out


class LimitlessMarketCollector(BaseCollector):
    name = "limitless_markets"

    def __init__(self, *, prices: Any, registry: Any, client: LimitlessClient | None = None, settings: Any = None):
        super().__init__()
        from core.config import get_settings

        self.settings = settings or get_settings()
        cfg = self.settings.limitless
        api_key = cfg.api_key.get_secret_value() if cfg.api_key else None
        api_secret = cfg.api_secret.get_secret_value() if cfg.api_secret else None
        self.client = client or LimitlessClient(api_key=api_key, api_secret=api_secret)
        self.prices = prices
        self.registry = registry
        self.candidates: list[LimitlessCandidate] = []
        from execution.limitless.onchain import PoolPricer

        self._pricer = PoolPricer()
        self._fpmm_addr: dict[str, str] = {}
        self._reprice_skip: dict[str, Any] = {}  # market_id -> riprova dopo (cache negativa)
        self._known_epics: set[str] = set()

    async def _reprice_placeholders(self, cfg: Any, now: Any, top_n: int = 40) -> None:
        """I mercati AMM col listino 50/50 (placeholder) vengono ri-prezzati dal pool on-chain.

        Cache negativa: chi risulta a prezzi estremi non viene ri-esaminato per 30 minuti,
        cosi' ogni scan esplora mercati nuovi invece di riscartare sempre gli stessi."""
        import asyncio as _aio
        from datetime import timedelta

        repriced = dropped = 0
        for cand in list(self.candidates):
            if cand.tokens or abs(cand.yes_price - 0.5) >= 0.005:
                continue
            retry_at = self._reprice_skip.get(cand.market_id)
            if retry_at is not None and now < retry_at:
                self.candidates.remove(cand)
                continue
            if repriced + dropped >= top_n:
                break
            try:
                addr = self._fpmm_addr.get(cand.market_id)
                if not addr:
                    m = await self.client.market(cand.slug) if cand.slug else None
                    addr = (m or {}).get("address")
                    if not addr:
                        raise RuntimeError("fpmm assente")
                    self._fpmm_addr[cand.market_id] = addr
                bid, offer = await _aio.to_thread(self._pricer.price, addr)
                mid = (bid + offer) / 2
                if not (cfg.min_price <= mid <= cfg.max_price):
                    self.candidates.remove(cand)
                    self._reprice_skip[cand.market_id] = now + timedelta(minutes=30)
                    dropped += 1
                    continue
                cand.yes_price = mid
                cand.priority = priority_of({"volume": cand.volume, "yes_price": mid}, cand.expiry, now)
                self.prices.push_live(Quote(epic=cand.epic, bid=bid, offer=offer, ts=now,
                                            market_status=MarketStatus.TRADEABLE, source="limitless-fpmm"))
                repriced += 1
            except Exception:  # noqa: BLE001 - prezzo non verificabile: fuori dai candidati
                if cand in self.candidates:
                    self.candidates.remove(cand)
                dropped += 1
        if repriced or dropped:
            self.candidates.sort(key=lambda c: c.priority, reverse=True)
            self.log.info("limitless.repriced", repriced=repriced, dropped=dropped)

    async def collect(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        cfg = self.settings.limitless
        now = utcnow()
        raw_markets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, cfg.max_pages + 1):
            batch = await self.client.active_markets(limit=25, page=page)
            fresh = [m for m in batch if str(m.get("id")) not in seen]
            for m in fresh:
                seen.add(str(m.get("id")))
            raw_markets.extend(fresh)
            if len(batch) < 25 or not fresh:
                break

        parsed_pairs = [(parse_market(m), m) for m in raw_markets]
        self.candidates = select_candidates(parsed_pairs, cfg, now)
        await self._reprice_placeholders(cfg, now)

        new_instruments = 0
        for cand in self.candidates:
            if cand.epic not in self._known_epics and self.registry.get(cand.epic) is None:
                self.registry.add(
                    Instrument(
                        epic=cand.epic,
                        name=cand.title[:200] or cand.epic,
                        asset_class=AssetClass.OTHER,
                        currency="USD",
                        market_status=MarketStatus.TRADEABLE,
                        min_size=1.0,
                        size_step=1.0,
                        value_per_point=1.0,
                        margin_factor=0.0,  # acquisto cash di quote: nessun margine (rischio = prezzo pagato)
                        spread=HALF_SPREAD * 2,
                        streaming_available=False,
                        expiry="-",
                        raw={"venue": "limitless", "market_id": cand.market_id, "slug": cand.slug,
                             "expiry_ts": cand.expiry.isoformat() if cand.expiry else None},
                    )
                )
                new_instruments += 1
            self._known_epics.add(cand.epic)
            bid = max(0.001, cand.yes_price - HALF_SPREAD)
            offer = min(0.999, cand.yes_price + HALF_SPREAD)
            self.prices.push_live(Quote(epic=cand.epic, bid=bid, offer=offer, ts=now,
                                        market_status=MarketStatus.TRADEABLE, source="limitless"))
        if new_instruments:
            await self.registry.save_to_db()

        async with session_scope() as session:
            repo = Repository(session)
            for parsed, raw in parsed_pairs:
                expiry = parse_expiry(raw)
                await repo.upsert_market(
                    "limitless", str(parsed["id"]),
                    slug=raw.get("slug"),
                    question=str(parsed.get("title") or "")[:2000],
                    category=(parsed.get("categories") or ["other"])[0].lower()[:40] if parsed.get("categories") else "other",
                    status=str(parsed.get("status") or "OPEN")[:30],
                    tradable=True,
                    volume=parsed.get("volume"),
                    resolution_date=expiry,
                    raw={"yes_price": parsed.get("yes_price"), "no_price": parsed.get("no_price"),
                         "tags": (parsed.get("tags") or [])[:10]},
                )
        self.log.info("limitless.scan", markets=len(raw_markets), candidates=len(self.candidates), new_instruments=new_instruments)
        return len(raw_markets)
