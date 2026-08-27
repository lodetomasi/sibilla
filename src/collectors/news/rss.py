"""News collector RSS + fonti ufficiali (sez. 11-13, 67).

Ogni item diventa NewsRecord con source/tier/reliability/published_at/retrieved_at,
viene deduplicato/clusterizzato, classificato per categoria e persistito. Le
news nuove emettono NEWS_DETECTED sul bus.
"""
from __future__ import annotations

import asyncio
from calendar import timegm
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from collectors.base import BaseCollector, CollectionMode
from collectors.news.dedup import NewsClusterer, domain_of, fingerprint
from collectors.news.sources import DEFAULT_FEEDS, FeedSource, tier_for_domain
from core.bus import emit
from core.clock import utcnow
from core.config import get_settings
from core.db import session_scope
from core.enums import Category, EventType
from core.repository import Repository
from core.schemas import NewsRecord
from market.categorization import classify, extract_entities, score_categories


class RSSNewsCollector(BaseCollector):
    name = "news_rss"

    def __init__(self, feeds: tuple[FeedSource, ...] | None = None, *, http: httpx.AsyncClient | None = None, concurrency: int = 8):
        super().__init__()
        settings = get_settings()
        self.feeds = feeds or DEFAULT_FEEDS
        self._http = http or httpx.AsyncClient(timeout=settings.news.fetch_timeout_s, headers={"User-Agent": settings.news.user_agent}, follow_redirects=True)
        self._own_http = http is None
        self._sem = asyncio.Semaphore(concurrency)
        self.max_items = settings.news.max_items_per_feed
        self.clusterer = NewsClusterer()
        self._feed_health: dict[str, dict[str, Any]] = {}

    async def collect(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        results = await asyncio.gather(*(self._fetch_feed(feed) for feed in self.feeds), return_exceptions=True)
        records: list[NewsRecord] = []
        for feed, result in zip(self.feeds, results, strict=False):
            if isinstance(result, Exception):
                self._feed_health[feed.name] = {"ok": False, "error": str(result)[:120]}
                continue
            self._feed_health[feed.name] = {"ok": True, "items": len(result)}
            records.extend(result)
        await self._ensure_sources()
        new_items = await self.store(records)
        self.stats.details["feeds_ok"] = sum(1 for h in self._feed_health.values() if h.get("ok"))
        self.stats.details["feeds_failed"] = [n for n, h in self._feed_health.items() if not h.get("ok")]
        self.stats.watermark = utcnow()
        return len(new_items)

    async def _fetch_feed(self, feed: FeedSource) -> list[NewsRecord]:
        async with self._sem:
            response = await self._http.get(feed.url)
        if response.status_code >= 400:
            raise RuntimeError(f"{feed.name}: HTTP {response.status_code}")
        parsed = feedparser.parse(response.content)
        records: list[NewsRecord] = []
        for entry in parsed.entries[: self.max_items]:
            record = entry_to_record(entry, feed)
            if record is not None:
                records.append(record)
        return records

    async def store(self, records: list[NewsRecord]) -> list[NewsRecord]:
        """Dedup + cluster + persist; ritorna solo le news nuove."""
        if not records:
            return []
        records.sort(key=lambda r: r.effective_ts)
        for record in records:
            self.clusterer.assign(record)
        async with session_scope() as session:
            repo = Repository(session)
            created = await repo.add_news([_record_to_row(r) for r in records])
            new_fps = {row.fingerprint for row in created}
        new_records = [r for r in records if r.fingerprint in new_fps]
        for record in new_records:
            await emit(
                EventType.NEWS_DETECTED,
                {
                    "fingerprint": record.fingerprint, "title": record.title, "url": record.url, "source": record.source_name,
                    "tier": record.tier.value, "reliability": record.reliability, "published_at": record.effective_ts.isoformat(),
                    "categories": [c.value for c in record.categories], "entities": record.entities, "cluster_id": record.cluster_id,
                    "is_original": record.is_original, "independent_confirmations": record.independent_confirmations,
                },
                source=self.name,
            )
        return new_records

    async def _ensure_sources(self) -> None:
        async with session_scope() as session:
            repo = Repository(session)
            for feed in self.feeds:
                await repo.upsert_source(feed.name, domain=feed.domain, source_type=feed.source_type, tier=feed.tier.value, reliability=feed.reliability, categories=[c.value for c in feed.categories], feed_url=feed.url, stats=self._feed_health.get(feed.name, {}))

    def health(self) -> dict[str, Any]:
        return dict(self._feed_health)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


def entry_to_record(entry: Any, feed: FeedSource) -> NewsRecord | None:
    title = (getattr(entry, "title", "") or "").strip()
    link = (getattr(entry, "link", "") or "").strip()
    if not title:
        return None
    published = _entry_time(entry)
    summary = (getattr(entry, "summary", "") or "")[:2000]
    domain = domain_of(link) if link else feed.domain
    tier = feed.tier if feed.domain and domain.endswith(feed.domain) else tier_for_domain(domain)
    text = f"{title} {summary}"
    categories = [c for c, _ in sorted(score_categories(text).items(), key=lambda kv: -kv[1])[:3]] or [classify(title)]
    for cat in feed.categories:
        if cat not in categories and len(categories) < 3:
            categories.append(cat)
    return NewsRecord(
        fingerprint=fingerprint(title, link),
        title=title,
        url=link or feed.url,
        source_name=feed.name,
        source_type=feed.source_type,
        tier=tier,
        reliability=tier.reliability,
        summary=summary or None,
        language=str(getattr(entry, "language", "") or "") or None,
        published_at=published,
        retrieved_at=utcnow(),
        entities=extract_entities(title),
        categories=[c if isinstance(c, Category) else Category(c) for c in categories],
        raw={"id": getattr(entry, "id", None), "tags": [t.get("term") for t in getattr(entry, "tags", []) or [] if isinstance(t, dict)][:10]},
    )


def _entry_time(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                return datetime.fromtimestamp(timegm(value), tz=UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def _record_to_row(record: NewsRecord) -> dict[str, Any]:
    return {
        "fingerprint": record.fingerprint, "cluster_id": record.cluster_id, "source_name": record.source_name, "source_type": record.source_type,
        "tier": record.tier.value, "reliability": record.reliability, "url": record.url, "title": record.title, "summary": record.summary,
        "body": record.body, "language": record.language, "published_at": record.published_at, "retrieved_at": record.retrieved_at,
        "is_confirmed": record.is_confirmed, "is_original": record.is_original, "independent_confirmations": record.independent_confirmations,
        "entities": record.entities, "categories": [c.value for c in record.categories], "raw": record.raw,
    }
