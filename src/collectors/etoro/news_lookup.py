"""Ricerca notizie recenti per uno strumento, da passare al judge anti pump&dump.

Riusa Repository.recent_news (popolato da RSSNewsCollector, avviato in
background da workers/etoro_runner.py::main()) invece di interrogare feed
esterni direttamente qui. Repository.recent_news fa un match a sottostringa
ESATTA su tutto il titolo (LIKE '%query%'): il nome eToro grezzo include quasi
sempre un suffisso societario (NV, Plc, Inc, ...) che una notizia reale quasi
mai ripete alla lettera - visto in produzione 28/8, "Ackermans & Van Haaren NV"
non trovava mai nulla anche con notizie vere presenti nel DB.
"""
from __future__ import annotations

from core.db import session_scope
from core.repository import Repository

_CORPORATE_SUFFIXES = frozenset({
    "inc", "inc.", "corp", "corp.", "corporation", "ltd", "ltd.", "limited",
    "plc", "nv", "sa", "s.a.", "ag", "se", "asa", "ab", "oyj", "co", "co.",
    "company", "spa", "s.p.a.",
})
# Primo termine troppo generico per un fallback a singola parola: rumore, non segnale.
_TOO_GENERIC_FIRST_WORD = frozenset({
    "american", "national", "general", "united", "global", "international", "first", "the",
})


def _core_name(instrument_name: str) -> str:
    """Il nome eToro spogliato dei suffissi societari finali (NV, Plc, Inc...)."""
    words = instrument_name.replace(",", "").split()
    while words and words[-1].lower() in _CORPORATE_SUFFIXES:
        words.pop()
    return " ".join(words) or instrument_name


async def recent_news_brief(instrument_name: str, *, minutes: int = 240, limit: int = 5) -> str:
    core = _core_name(instrument_name)
    async with session_scope(write=False) as session:
        repo = Repository(session)
        rows = await repo.recent_news(minutes=minutes, query=core, limit=limit)
        if not rows:
            words = core.split()
            first_word = words[0] if words else core
            if (
                len(words) > 1
                and len(first_word) >= 4
                and first_word.lower() not in _TOO_GENERIC_FIRST_WORD
            ):
                rows = await repo.recent_news(minutes=minutes, query=first_word, limit=limit)
    if not rows:
        return ""
    lines = [f"- {r.title} ({r.source_name}, {r.published_at or r.retrieved_at})" for r in rows]
    return "\n".join(lines)
