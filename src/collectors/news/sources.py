"""Registry fonti (sez. 11/12): tier, affidabilita, feed RSS. Social = secondario (TIER_5)."""
from __future__ import annotations

from dataclasses import dataclass

from core.enums import Category, SourceTier


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tier: SourceTier
    source_type: str
    categories: tuple[Category, ...]
    domain: str = ""

    @property
    def reliability(self) -> float:
        return self.tier.reliability


# Fonti ufficiali e agenzie con RSS pubblico stabile.
DEFAULT_FEEDS: tuple[FeedSource, ...] = (
    # TIER 1 - fonti ufficiali
    FeedSource("BLS CPI", "https://www.bls.gov/feed/cpi.rss", SourceTier.TIER_1, "official", (Category.ECONOMICS, Category.MACRO), "bls.gov"),
    FeedSource("BLS Employment Situation", "https://www.bls.gov/feed/empsit.rss", SourceTier.TIER_1, "official", (Category.ECONOMICS, Category.MACRO), "bls.gov"),
    FeedSource("BLS PPI", "https://www.bls.gov/feed/ppi.rss", SourceTier.TIER_1, "official", (Category.ECONOMICS,), "bls.gov"),
    FeedSource("BLS News Releases", "https://www.bls.gov/feed/bls_latest.rss", SourceTier.TIER_1, "official", (Category.ECONOMICS, Category.MACRO), "bls.gov"),
    FeedSource("Federal Reserve Press", "https://www.federalreserve.gov/feeds/press_all.xml", SourceTier.TIER_1, "official", (Category.MACRO,), "federalreserve.gov"),
    FeedSource("Federal Reserve Monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml", SourceTier.TIER_1, "official", (Category.MACRO,), "federalreserve.gov"),
    FeedSource("ECB Press", "https://www.ecb.europa.eu/rss/press.html", SourceTier.TIER_1, "official", (Category.MACRO,), "ecb.europa.eu"),
    FeedSource("BEA News", "https://apps.bea.gov/rss/rss.xml", SourceTier.TIER_1, "official", (Category.ECONOMICS, Category.MACRO), "bea.gov"),
    FeedSource("SEC Press", "https://www.sec.gov/news/pressreleases.rss", SourceTier.TIER_1, "official", (Category.COMPANIES,), "sec.gov"),
    FeedSource("Bank of England", "https://www.bankofengland.co.uk/rss/news", SourceTier.TIER_1, "official", (Category.MACRO,), "bankofengland.co.uk"),
    # TIER 2 - agenzie (Reuters/AP non espongono piu RSS pubblici stabili: coperti via WSJ/CNBC/FT/BBC)
    FeedSource("WSJ World", "https://feeds.a.dj.com/rss/RSSWorldNews.xml", SourceTier.TIER_2, "agency", (Category.GEOPOLITICS, Category.POLITICS), "wsj.com"),
    FeedSource("WSJ Economy", "https://feeds.a.dj.com/rss/RSSWSJD.xml", SourceTier.TIER_2, "agency", (Category.ECONOMICS, Category.MACRO), "wsj.com"),
    FeedSource("Fed Speeches", "https://www.federalreserve.gov/feeds/speeches.xml", SourceTier.TIER_1, "official", (Category.MACRO,), "federalreserve.gov"),
    FeedSource("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml", SourceTier.TIER_1, "official", (Category.ECONOMICS,), "eia.gov"),
    # TIER 3 - media reputati
    FeedSource("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES, Category.MACRO), "cnbc.com"),
    FeedSource("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.MACRO), "cnbc.com"),
    FeedSource("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES), "cnbc.com"),
    FeedSource("MarketWatch Top", "https://feeds.content.dowjones.io/public/rss/mw_topstories", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES), "marketwatch.com"),
    FeedSource("MarketWatch Realtime", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES, Category.MACRO), "marketwatch.com"),
    FeedSource("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES), "wsj.com"),
    FeedSource("FT Home", "https://www.ft.com/rss/home", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.MACRO, Category.GEOPOLITICS), "ft.com"),
    FeedSource("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES), "bbc.co.uk"),
    FeedSource("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", SourceTier.TIER_3, "media", (Category.GEOPOLITICS, Category.POLITICS), "bbc.co.uk"),
    FeedSource("Guardian Business", "https://www.theguardian.com/uk/business/rss", SourceTier.TIER_3, "media", (Category.ECONOMICS,), "theguardian.com"),
    FeedSource("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.COMPANIES), "yahoo.com"),
    FeedSource("Investing.com News", "https://www.investing.com/rss/news.rss", SourceTier.TIER_3, "media", (Category.ECONOMICS, Category.MACRO), "investing.com"),
    FeedSource("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", SourceTier.TIER_3, "media", (Category.CRYPTO,), "coindesk.com"),
    FeedSource("Cointelegraph", "https://cointelegraph.com/rss", SourceTier.TIER_3, "media", (Category.CRYPTO,), "cointelegraph.com"),
    FeedSource("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", SourceTier.TIER_3, "media", (Category.GEOPOLITICS,), "aljazeera.com"),
    # TIER 4 - specialisti
    FeedSource("ZeroHedge", "https://feeds.feedburner.com/zerohedge/feed", SourceTier.TIER_4, "specialist", (Category.MACRO, Category.ECONOMICS), "zerohedge.com"),
    FeedSource("Calculated Risk", "https://www.calculatedriskblog.com/feeds/posts/default", SourceTier.TIER_4, "specialist", (Category.ECONOMICS,), "calculatedriskblog.com"),
    # TIER 5 - social (secondario)
    FeedSource("Reddit r/economics", "https://www.reddit.com/r/Economics/new/.rss", SourceTier.TIER_5, "social", (Category.ECONOMICS,), "reddit.com"),
    # Comunicati stampa aziendali diretti (non giornalismo indipendente, per questo
    # TIER_4): tutte le altre fonti sopra sono macro/mega-cap, il judge anti
    # pump&dump del motore eToro (gap+volume su small/mid-cap) restava quasi sempre
    # senza notizie da verificare - verificato feed reale 28/8 (FDA, M&A, leadership).
    FeedSource("PR Newswire Public Companies", "https://www.prnewswire.com/rss/all-public-company-news-list.rss", SourceTier.TIER_4, "press_release", (Category.COMPANIES,), "prnewswire.com"),
)

OFFICIAL_DOMAINS: dict[str, SourceTier] = {
    "bls.gov": SourceTier.TIER_1, "federalreserve.gov": SourceTier.TIER_1, "ecb.europa.eu": SourceTier.TIER_1, "bea.gov": SourceTier.TIER_1,
    "sec.gov": SourceTier.TIER_1, "treasury.gov": SourceTier.TIER_1, "whitehouse.gov": SourceTier.TIER_1, "bankofengland.co.uk": SourceTier.TIER_1,
    "boj.or.jp": SourceTier.TIER_1, "imf.org": SourceTier.TIER_1, "census.gov": SourceTier.TIER_1, "eia.gov": SourceTier.TIER_1,
    "reuters.com": SourceTier.TIER_2, "apnews.com": SourceTier.TIER_2, "bloomberg.com": SourceTier.TIER_2, "afp.com": SourceTier.TIER_2,
    "cnbc.com": SourceTier.TIER_3, "wsj.com": SourceTier.TIER_3, "ft.com": SourceTier.TIER_3, "marketwatch.com": SourceTier.TIER_3,
    "bbc.co.uk": SourceTier.TIER_3, "nytimes.com": SourceTier.TIER_3, "theguardian.com": SourceTier.TIER_3, "coindesk.com": SourceTier.TIER_3,
    "reddit.com": SourceTier.TIER_5, "x.com": SourceTier.TIER_5, "twitter.com": SourceTier.TIER_5, "t.me": SourceTier.TIER_5,
}


def tier_for_domain(domain: str) -> SourceTier:
    domain = domain.lower()
    for known, tier in OFFICIAL_DOMAINS.items():
        if domain == known or domain.endswith("." + known):
            return tier
    return SourceTier.TIER_4
