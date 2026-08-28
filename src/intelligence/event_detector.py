"""Event Detection (patch sez. 38): trasforma segnali grezzi in DetectedEvent.

Sorgenti: NEWS_DETECTED (news collector), MACRO_RELEASE (calendario/BLS),
POLYMARKET_REPRICING (variazione di probabilita rilevante), ANOMALY_DETECTED,
WALLET_TRADE cluster. Ogni evento e' persistito e ha evidenze con fonte+timestamp.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from core.bus import BusEvent, emit
from core.clock import utcnow
from core.db import session_scope
from core.enums import Category, EventType, EvidenceDirection, EvidenceType, SourceTier
from core.logging import get_logger
from core.repository import Repository
from core.schemas import DetectedEvent, Evidence, MacroRelease
from market.categorization import classify, extract_entities

log = get_logger("intelligence.event_detector")


_FINANCIAL_CATEGORIES = {
    Category.MACRO.value, Category.ECONOMICS.value, Category.POLITICS.value, Category.GEOPOLITICS.value,
    Category.CRYPTO.value, Category.COMPANIES.value, Category.TECHNOLOGY.value,
}


def _event_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


class EventDetector:
    def __init__(self, *, polymarket_min_change_pp: float = 8.0, polymarket_window_minutes: int = 60, min_news_tier: SourceTier = SourceTier.TIER_4):
        self.polymarket_min_change_pp = polymarket_min_change_pp
        self.polymarket_window_minutes = polymarket_window_minutes
        self.min_news_tier = min_news_tier
        self._seen: set[str] = set()

    # ------------------------------------------------------------ handlers
    async def on_news(self, event: BusEvent) -> DetectedEvent | None:
        payload = event.payload
        tier = SourceTier(payload.get("tier", "TIER_3"))
        if _tier_rank(tier) > _tier_rank(self.min_news_tier):
            return None
        if not payload.get("is_original", True):
            return None  # copie sindacate non generano nuovi eventi (sez. 67)
        published = payload.get("published_at")
        occurred = _parse(published) if published else event.ts
        if occurred and (utcnow() - occurred) > timedelta(hours=6):
            return None  # troppo vecchia per un edge event-driven
        event_id = _event_id("news", payload.get("cluster_id") or payload.get("fingerprint"))
        if event_id in self._seen:
            return None
        categories = payload.get("categories") or []
        category = Category(categories[0]) if categories else classify(payload.get("title", ""))
        # Disciplina di costo/edge: solo categorie con canale causale verso CFD tradabili.
        # Sport/intrattenimento/meteo/generico NON diventano eventi -> niente spesa LLM su rumore.
        if not any(c in _FINANCIAL_CATEGORIES for c in ([category.value] + list(categories))):
            return None
        # Notizie non ufficiali e non confermate devono almeno nominare un'entita rilevante,
        # altrimenti sono cronaca generica: si scartano prima dell'LLM.
        confirmed = tier is SourceTier.TIER_1 or int(payload.get("independent_confirmations", 0)) >= 1
        entities = list(payload.get("entities") or extract_entities(str(payload.get("title", ""))))
        if not confirmed and _tier_rank(tier) >= 3 and not entities:
            return None
        evidence = Evidence(
            evidence_id=str(payload.get("fingerprint")), type=EvidenceType.OFFICIAL if tier is SourceTier.TIER_1 else EvidenceType.NEWS,
            source=str(payload.get("source")), source_tier=tier, url=payload.get("url"), timestamp=occurred or utcnow(),
            reliability=float(payload.get("reliability", tier.reliability)), direction=EvidenceDirection.SUPPORT, impact=0.5,
            is_confirmed=tier is SourceTier.TIER_1 or int(payload.get("independent_confirmations", 0)) >= 1,
            independent_confirmations=int(payload.get("independent_confirmations", 0)), summary=str(payload.get("title", ""))[:300],
        )
        detected = DetectedEvent(
            event_id=event_id, kind="NEWS", title=str(payload.get("title", "")), category=category, detected_at=utcnow(), occurred_at=occurred,
            evidence=[evidence], entities=entities,
            source_reliability=evidence.reliability, is_verified=evidence.is_confirmed, raw={"cluster_id": payload.get("cluster_id")},
        )
        await self._persist_and_emit(detected)
        return detected

    async def on_macro(self, event: BusEvent) -> DetectedEvent | None:
        release = MacroRelease.model_validate(event.payload)
        event_id = _event_id("macro", release.indicator.value, release.country, release.release_time.isoformat())
        if event_id in self._seen:
            return None
        surprise = release.surprise if release.surprise is not None else ((release.actual - release.previous) if release.actual is not None and release.previous is not None else None)
        evidence = Evidence(
            evidence_id=event_id, type=EvidenceType.MACRO_DATA, source=release.source or "macro_calendar", source_tier=SourceTier.TIER_1 if release.source and any(k in release.source.lower() for k in ("bls", "fred", "bea", "fed")) else SourceTier.TIER_2,
            url=release.url, timestamp=release.release_time, reliability=0.95, impact=0.8 if surprise else 0.4, is_confirmed=release.actual is not None,
            summary=f"{release.name}: actual={release.actual} consensus={release.consensus} previous={release.previous} {release.unit}",
        )
        detected = DetectedEvent(
            event_id=event_id, kind="MACRO_RELEASE", title=f"{release.country} {release.indicator.value}: {release.actual} vs consensus {release.consensus} (prev {release.previous})",
            summary=evidence.summary, category=Category.MACRO, detected_at=utcnow(), occurred_at=release.release_time, evidence=[evidence],
            entities=[release.indicator.value, release.country], surprise=surprise, macro=release, source_reliability=evidence.reliability, is_verified=release.actual is not None,
        )
        await self._persist_and_emit(detected)
        return detected

    async def on_anomaly(self, event: BusEvent) -> DetectedEvent | None:
        payload = event.payload
        if not payload.get("requires_investigation") and float(payload.get("severity", 0)) < 0.7:
            return None
        event_id = _event_id("anomaly", payload.get("market_external_id"), payload.get("kind"), str(payload.get("ts", ""))[:16])
        if event_id in self._seen:
            return None
        async with session_scope() as session:
            market = await Repository(session).get_market("polymarket", str(payload.get("market_external_id")))
        if market is not None and market.category not in _FINANCIAL_CATEGORIES:
            return None  # sport/intrattenimento/meteo: nessun canale causale verso CFD tradabili
        title = f"Anomalia {payload.get('kind')} su {market.question if market else payload.get('market_external_id')}"
        evidence = Evidence(evidence_id=event_id, type=EvidenceType.MARKET, source="polymarket", source_tier=SourceTier.TIER_2, timestamp=_parse(payload.get("ts")) or utcnow(), reliability=0.8, impact=float(payload.get("severity", 0.5)), summary=str(payload.get("details"))[:300], details=payload.get("details") or {})
        detected = DetectedEvent(event_id=event_id, kind="ANOMALY", title=title, category=Category(market.category) if market else Category.OTHER, evidence=[evidence], polymarket_market_id=str(payload.get("market_external_id")), source_reliability=0.8, is_verified=False, raw=payload)
        await self._persist_and_emit(detected)
        return detected

    async def scan_polymarket_repricing(self, *, limit_markets: int = 150) -> list[DetectedEvent]:
        """Polymarket -> Financial Asset Signal (patch sez. 31.B): salti di probabilita rilevanti."""
        detected: list[DetectedEvent] = []
        since = utcnow() - timedelta(minutes=self.polymarket_window_minutes)
        async with session_scope() as session:
            repo = Repository(session)
            markets = await repo.list_markets(venue="polymarket", status="OPEN", limit=limit_markets)
            for market in markets:
                if market.category not in (Category.MACRO.value, Category.ECONOMICS.value, Category.POLITICS.value, Category.GEOPOLITICS.value, Category.CRYPTO.value, Category.COMPANIES.value):
                    continue
                rows = await repo.price_history(market.id, outcome="Yes", since=since, limit=500)
                if len(rows) < 2:
                    continue
                first, last = rows[0].price, rows[-1].price
                change_pp = (last - first) * 100
                if abs(change_pp) < self.polymarket_min_change_pp:
                    continue
                bucket = rows[-1].ts.replace(minute=(rows[-1].ts.minute // 15) * 15, second=0, microsecond=0)
                event_id = _event_id("pm", market.external_id, bucket.isoformat())
                if event_id in self._seen:
                    continue
                evidence = Evidence(evidence_id=event_id, type=EvidenceType.POLYMARKET, source="polymarket", source_tier=SourceTier.TIER_2, url=f"https://polymarket.com/market/{market.slug}" if market.slug else None, timestamp=rows[-1].ts, reliability=0.75, impact=min(1.0, abs(change_pp) / 30), summary=f"{market.question}: {first:.0%} -> {last:.0%} in {self.polymarket_window_minutes}m", details={"from": first, "to": last, "volume": market.volume})
                event = DetectedEvent(event_id=event_id, kind="POLYMARKET_REPRICING", title=f"Polymarket repricing: {market.question} ({first:.0%} -> {last:.0%})", category=Category(market.category), occurred_at=rows[-1].ts, evidence=[evidence], entities=extract_entities(market.question), polymarket_probability_change=change_pp / 100, polymarket_market_id=market.external_id, source_reliability=0.75, is_verified=False, raw={"question": market.question, "volume": market.volume})
                detected.append(event)
        for event in detected:
            await self._persist_and_emit(event)
        return detected

    # ------------------------------------------------------------ persist
    async def _persist_and_emit(self, event: DetectedEvent) -> None:
        self._seen.add(event.event_id)
        if len(self._seen) > 20000:
            self._seen = set(list(self._seen)[-10000:])
        async with session_scope() as session:
            await Repository(session).upsert_detected_event(
                event.event_id, kind=event.kind, title=event.title, summary=event.summary, category=event.category.value, detected_at=event.detected_at,
                occurred_at=event.occurred_at, source_reliability=event.source_reliability, is_verified=event.is_verified, surprise=event.surprise,
                polymarket_probability_change=event.polymarket_probability_change, polymarket_market_id=event.polymarket_market_id,
                evidence=[e.model_dump(mode="json") for e in event.evidence], entities=event.entities, raw=event.raw, status="NEW",
            )
        await emit(EventType.EVENT_DETECTED, {"event_id": event.event_id, "kind": event.kind, "title": event.title, "category": event.category.value}, source="event_detector")
        log.info("event.detected", kind=event.kind, title=event.title[:100], reliability=event.source_reliability)


def _tier_rank(tier: SourceTier) -> int:
    return int(tier.value.split("_")[1])


def _parse(value: Any):
    from datetime import datetime

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    from datetime import UTC

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
