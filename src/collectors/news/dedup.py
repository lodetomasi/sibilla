"""News deduplication (sez. 67): la stessa notizia su 20 siti non e' 20 conferme.

Fingerprint per URL/titolo normalizzato; cluster per similarita del titolo
(token set + rapidfuzz). Le conferme indipendenti contano solo fonti/domini diversi.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from rapidfuzz import fuzz

from core.schemas import NewsRecord
from market.categorization import normalize_text, token_set

_TRACKING = re.compile(r"(utm_[a-z]+|fbclid|gclid|ref|source)=[^&]*&?")


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = _TRACKING.sub("", parsed.query).strip("&")
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc.lower().removeprefix('www.')}{path}{'?' + query if query else ''}"


def fingerprint(title: str, url: str) -> str:
    base = canonical_url(url) if url else normalize_text(title)
    return hashlib.sha256(base.encode()).hexdigest()[:40]


def title_fingerprint(title: str) -> str:
    return hashlib.sha256(normalize_text(title).encode()).hexdigest()[:40]


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def similar(a: str, b: str, *, threshold: float = 82.0) -> bool:
    if not a or not b:
        return False
    ta, tb = token_set(a), token_set(b)
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        if jaccard >= 0.6:
            return True
    return fuzz.token_set_ratio(normalize_text(a), normalize_text(b)) >= threshold


class NewsClusterer:
    """Raggruppa news simili in cluster; il primo per published_at e' l'originale."""

    def __init__(self, existing: list[NewsRecord] | None = None):
        self._clusters: dict[str, list[NewsRecord]] = {}
        for record in existing or []:
            if record.cluster_id:
                self._clusters.setdefault(record.cluster_id, []).append(record)

    def assign(self, record: NewsRecord) -> NewsRecord:
        for cluster_id, members in self._clusters.items():
            head = members[0]
            if similar(record.title, head.title):
                members.append(record)
                domains = {domain_of(m.url) for m in members}
                earliest = min(members, key=lambda m: m.effective_ts)
                for member in members:
                    member.cluster_id = cluster_id
                    member.is_original = member is earliest
                    member.independent_confirmations = max(0, len(domains) - 1)
                    member.is_confirmed = member.is_confirmed or member.independent_confirmations >= 1 or member.tier.value == "TIER_1"
                return record
        cluster_id = title_fingerprint(record.title)
        record.cluster_id = cluster_id
        record.is_original = True
        record.independent_confirmations = 0
        record.is_confirmed = record.tier.value == "TIER_1"
        self._clusters[cluster_id] = [record]
        return record

    def cluster(self, cluster_id: str) -> list[NewsRecord]:
        return list(self._clusters.get(cluster_id, []))

    def confirmations(self, cluster_id: str) -> dict[str, object]:
        members = self._clusters.get(cluster_id, [])
        if not members:
            return {"original_source": None, "syndicated_sources": [], "independent_confirmations": 0}
        earliest = min(members, key=lambda m: m.effective_ts)
        domains = sorted({domain_of(m.url) for m in members})
        return {
            "original_source": earliest.source_name,
            "syndicated_sources": [m.source_name for m in members if m is not earliest],
            "independent_confirmations": max(0, len(domains) - 1),
            "highest_tier": min(members, key=lambda m: m.tier.value).tier.value,
        }
