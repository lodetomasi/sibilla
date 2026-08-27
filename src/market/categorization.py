"""Classificazione in categorie (sez. 5.3) ed estrazione entita.

Deterministica e senza LLM: serve a classificare ogni trade/mercato/news anche
quando il budget LLM e chiuso, e a rendere riproducibile la categorizzazione
usata nei ranking wallet (che devono essere point-in-time).
"""
from __future__ import annotations

import re
from collections import Counter

from core.enums import Category

_KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.CRYPTO: (
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol ", "crypto", "token", "altcoin",
        "defi", "stablecoin", "binance", "coinbase", "ripple", "xrp", "dogecoin", "memecoin",
        "halving", "etf bitcoin", "onchain", "satoshi", "airdrop",
    ),
    Category.POLITICS: (
        "election", "elections", "president", "presidential", "senate", "congress", "parliament",
        "prime minister", "governor", "mayor", "vote", "voter", "ballot", "primary", "candidate",
        "impeach", "cabinet", "referendum", "coalition", "chancellor", "nominee", "poll",
    ),
    Category.SPORTS: (
        " vs ", " v ", "match", "premier league", "la liga", "serie a", "bundesliga", "champions",
        "nba", "nfl", "mlb", "nhl", "ufc", "boxing", "tennis", "atp", "wta", "world cup",
        "olympic", "formula 1", "f1 ", "grand prix", "golf", "pga", "cricket", "super bowl",
        "playoff", "goal", "score", "win against", "beat", "tournament", "seed",
    ),
    Category.MACRO: (
        "fed ", "federal reserve", "fomc", "interest rate", "rate cut", "rate hike", "ecb",
        "bank of england", "boj", "recession", "yield curve", "treasury", "qe", "tapering",
    ),
    Category.ECONOMICS: (
        "inflation", "cpi", "ppi", "gdp", "unemployment", "jobless", "payroll", "nfp",
        "retail sales", "pmi", "consumer confidence", "trade deficit", "budget deficit",
        "minimum wage", "tariff",
    ),
    Category.TECHNOLOGY: (
        "ai ", "artificial intelligence", "openai", "anthropic", "gpt", "llm", "chip",
        "semiconductor", "nvidia", "apple", "google", "microsoft", "software", "app store",
        "quantum", "robotaxi", "starship", "spacex", "satellite", "model release",
    ),
    Category.GEOPOLITICS: (
        "war", "ceasefire", "invasion", "nato", "sanction", "treaty", "border", "missile",
        "strike", "hostage", "peace deal", "military", "coup", "un security council", "embargo",
    ),
    Category.COMPANIES: (
        "earnings", "ipo", "merger", "acquisition", "ceo", "bankruptcy", "layoff", "revenue",
        "guidance", "buyback", "dividend", "stock split", "delisting", "sec filing",
    ),
    Category.SCIENCE: (
        "vaccine", "clinical trial", "fda approval", "nobel", "study finds", "peer review",
        "gene", "crispr", "fusion", "telescope", "asteroid", "mars mission", "pandemic",
    ),
    Category.ENTERTAINMENT: (
        "oscar", "academy award", "grammy", "emmy", "box office", "movie", "album", "netflix",
        "billboard", "eurovision", "celebrity", "tour dates", "rotten tomatoes",
    ),
    Category.WEATHER: (
        "hurricane", "temperature", "rainfall", "snow", "el nino", "la nina", "heatwave",
        "drought", "wildfire", "tornado", "typhoon", "climate", "flood",
    ),
}

_TAG_MAP: dict[str, Category] = {
    "crypto": Category.CRYPTO, "bitcoin": Category.CRYPTO, "ethereum": Category.CRYPTO,
    "politics": Category.POLITICS, "us-politics": Category.POLITICS,
    "elections": Category.POLITICS, "geopolitics": Category.GEOPOLITICS,
    "sports": Category.SPORTS, "soccer": Category.SPORTS, "football": Category.SPORTS,
    "nba": Category.SPORTS, "nfl": Category.SPORTS, "epl": Category.SPORTS,
    "tennis": Category.SPORTS, "mma": Category.SPORTS, "baseball": Category.SPORTS,
    "economy": Category.ECONOMICS, "economics": Category.ECONOMICS, "inflation": Category.ECONOMICS,
    "fed": Category.MACRO, "macro": Category.MACRO, "rates": Category.MACRO,
    "tech": Category.TECHNOLOGY, "technology": Category.TECHNOLOGY, "ai": Category.TECHNOLOGY,
    "business": Category.COMPANIES, "companies": Category.COMPANIES, "earnings": Category.COMPANIES,
    "science": Category.SCIENCE, "health": Category.SCIENCE, "covid": Category.SCIENCE,
    "pop-culture": Category.ENTERTAINMENT, "entertainment": Category.ENTERTAINMENT,
    "movies": Category.ENTERTAINMENT, "music": Category.ENTERTAINMENT,
    "weather": Category.WEATHER, "climate": Category.WEATHER,
    "middle-east": Category.GEOPOLITICS, "ukraine": Category.GEOPOLITICS,
    "war": Category.GEOPOLITICS,
}

_STOPWORDS = {
    "will", "the", "and", "for", "with", "that", "this", "have", "has", "from", "not", "any",
    "who", "what", "when", "which", "than", "then", "there", "their", "before", "after", "over",
    "under", "into", "about", "more", "most", "less", "least", "many", "much", "does", "did",
    "win", "wins", "beat", "vs", "v", "match", "odds", "market", "yes", "no", "be", "in", "on",
    "at", "to", "of", "a", "an", "by", "is", "are", "was", "were", "it", "its", "or",
}


def classify(text: str, tags: list[str] | None = None) -> Category:
    """Categoria di un testo (question, titolo news, descrizione mercato).

    I tag espliciti del venue hanno priorita sulle keyword.
    """
    for tag in tags or []:
        key = str(tag).strip().lower().replace(" ", "-")
        if key in _TAG_MAP:
            return _TAG_MAP[key]
    scores = score_categories(text)
    if not scores:
        return Category.OTHER
    return max(scores.items(), key=lambda kv: kv[1])[0]


def score_categories(text: str) -> dict[Category, float]:
    lowered = f" {(text or '').lower()} "
    scores: dict[Category, float] = {}
    for category, keywords in _KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in lowered)
        if hits:
            scores[category] = float(hits)
    return scores


_TEAM_SEPARATORS = re.compile(r"\s+(?:vs\.?|v\.?|against|-)\s+", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9'&\.]+")


def extract_entities(text: str, *, max_entities: int = 8) -> list[str]:
    """Estrae entita candidate (nomi propri, squadre, ticker) senza NLP esterno."""
    if not text:
        return []
    cleaned = re.sub(r"[\?\!\"“”]", " ", text)
    entities: list[str] = []

    # pattern "A vs B" tipico dei mercati sportivi
    parts = _TEAM_SEPARATORS.split(cleaned)
    if len(parts) >= 2:
        for part in parts[:2]:
            candidate = " ".join(
                w for w in _WORD.findall(part) if w.lower() not in _STOPWORDS
            ).strip()
            if candidate:
                entities.append(candidate)

    for match in re.finditer(r"\b([A-Z][a-zA-Z'\.]{2,}(?:\s+[A-Z][a-zA-Z'\.]{2,}){0,2})", cleaned):
        candidate = match.group(1).strip()
        if candidate.lower() in _STOPWORDS:
            continue
        entities.append(candidate)

    seen: set[str] = set()
    unique: list[str] = []
    for entity in entities:
        key = entity.lower()
        if key in seen or len(key) < 3:
            continue
        seen.add(key)
        unique.append(entity)
    return unique[:max_entities]


def normalize_text(text: str) -> str:
    """Normalizzazione usata dal matcher (sez. 14)."""
    lowered = (text or "").lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    tokens = [t for t in lowered.split() if t not in _STOPWORDS and len(t) > 1]
    return " ".join(tokens)


def token_set(text: str) -> set[str]:
    return set(normalize_text(text).split())


def dominant_category(texts: list[str], tags: list[str] | None = None) -> Category:
    counter: Counter[Category] = Counter()
    for text in texts:
        counter[classify(text, tags)] += 1
    if not counter:
        return Category.OTHER
    return counter.most_common(1)[0][0]
