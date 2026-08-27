"""Recupero fonte ufficiale (patch sez. 38 SOURCE VERIFICATION, tool get_official_source).

Scarica una pagina e ne estrae il testo principale; i domini in OFFICIAL_DOMAINS
ricevono TIER_1/2. Nessuna esecuzione JS, solo HTML statico.
"""
from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from collectors.news.dedup import domain_of
from collectors.news.sources import tier_for_domain
from core.clock import utcnow
from core.config import get_settings
from core.logging import get_logger

log = get_logger("collectors.news.official")


async def fetch_official_source(url: str, *, http: httpx.AsyncClient | None = None, max_chars: int = 6000) -> dict[str, Any]:
    settings = get_settings()
    client = http or httpx.AsyncClient(timeout=settings.news.fetch_timeout_s, headers={"User-Agent": settings.news.user_agent}, follow_redirects=True)
    try:
        response = await client.get(url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return {"url": url, "ok": False, "error": str(exc)[:200], "retrieved_at": utcnow().isoformat()}
    finally:
        if http is None:
            await client.aclose()
    if response.status_code >= 400:
        return {"url": url, "ok": False, "error": f"HTTP {response.status_code}", "retrieved_at": utcnow().isoformat()}
    domain = domain_of(str(response.url))
    tier = tier_for_domain(domain)
    text, title, published = extract_text(response.text)
    return {
        "url": str(response.url), "ok": True, "domain": domain, "tier": tier.value, "reliability": tier.reliability,
        "title": title, "published_at": published, "retrieved_at": utcnow().isoformat(), "text": text[:max_chars],
        "text_length": len(text),
    }


def extract_text(html: str) -> tuple[str, str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    published = None
    for selector in ("meta[property='article:published_time']", "meta[name='pubdate']", "meta[name='date']", "time[datetime]"):
        node = soup.select_one(selector)
        if node is not None:
            published = node.get("content") or node.get("datetime")
            if published:
                break
    main = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in main.find_all(["p", "li", "h1", "h2", "h3", "td"])]
    text = "\n".join(p for p in paragraphs if len(p) > 30) or main.get_text(" ", strip=True)
    return text, title, str(published) if published else None
