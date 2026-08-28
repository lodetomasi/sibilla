from __future__ import annotations

from datetime import timedelta

from core.clock import utcnow
from core.db import session_scope
from core.repository import Repository
from collectors.etoro.news_lookup import recent_news_brief


async def _seed_news(*, title: str, source_name: str = "Reuters", minutes_ago: float = 10.0) -> None:
    async with session_scope() as session:
        await Repository(session).add_news([
            {
                "fingerprint": f"fp-{title}",
                "source_name": source_name,
                "url": "https://example.com/x",
                "title": title,
                "published_at": utcnow() - timedelta(minutes=minutes_ago),
            }
        ])


async def test_recent_news_brief_returns_empty_when_no_news(engine, memory_cache) -> None:
    brief = await recent_news_brief("PennyCo")
    assert brief == ""


async def test_recent_news_brief_formats_matching_news(engine, memory_cache) -> None:
    await _seed_news(title="PennyCo wins FDA approval for new drug")
    await _seed_news(title="Unrelated company news")

    brief = await recent_news_brief("PennyCo")

    assert "PennyCo wins FDA approval" in brief
    assert "Unrelated company news" not in brief
    assert "Reuters" in brief


async def test_recent_news_brief_respects_limit(engine, memory_cache) -> None:
    for i in range(3):
        await _seed_news(title=f"PennyCo update {i}")

    brief = await recent_news_brief("PennyCo", limit=2)

    assert brief.count("PennyCo update") == 2
