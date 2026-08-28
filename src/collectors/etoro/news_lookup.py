"""Ricerca notizie recenti per uno strumento, da passare al judge anti pump&dump.

Riusa Repository.recent_news (popolato da RSSNewsCollector, avviato in
background da workers/etoro_runner.py::main()) invece di interrogare feed
esterni direttamente qui.
"""
from __future__ import annotations

from core.db import session_scope
from core.repository import Repository


async def recent_news_brief(instrument_name: str, *, minutes: int = 240, limit: int = 5) -> str:
    async with session_scope(write=False) as session:
        rows = await Repository(session).recent_news(minutes=minutes, query=instrument_name, limit=limit)
    if not rows:
        return ""
    lines = [f"- {r.title} ({r.source_name}, {r.published_at or r.retrieved_at})" for r in rows]
    return "\n".join(lines)
