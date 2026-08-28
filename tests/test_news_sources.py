from __future__ import annotations

from collectors.news.sources import DEFAULT_FEEDS
from core.enums import Category, SourceTier


def test_pr_newswire_public_companies_feed_is_registered() -> None:
    # Aggiunta 28/8: le altre fonti sono tutte macro/mega-cap, il judge anti
    # pump&dump del motore eToro (small/mid-cap) restava quasi sempre senza
    # notizie da verificare - serve un feed di comunicati stampa aziendali.
    matches = [f for f in DEFAULT_FEEDS if f.name == "PR Newswire Public Companies"]

    assert len(matches) == 1
    feed = matches[0]
    assert feed.url == "https://www.prnewswire.com/rss/all-public-company-news-list.rss"
    assert feed.tier == SourceTier.TIER_4
    assert Category.COMPANIES in feed.categories
    assert feed.domain == "prnewswire.com"


def test_no_duplicate_feed_names() -> None:
    names = [f.name for f in DEFAULT_FEEDS]
    assert len(names) == len(set(names))
