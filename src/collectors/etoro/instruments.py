"""Universo strumenti eToro: azioni sotto soglia prezzo, cache 6h su file.

Iron rule: nessun prezzo inventato, il filtro usa SOLO currentRate dalla
risposta search (fonte esplicita, come le Quote altrove nel sistema).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.clock import utcnow
from core.logging import get_logger

log = get_logger("collectors.etoro.instruments")


@dataclass
class InstrumentCandidate:
    instrument_id: int
    name: str
    price: float

    def as_dict(self) -> dict[str, Any]:
        return {"instrument_id": self.instrument_id, "name": self.name, "price": self.price}


class InstrumentUniverse:
    def __init__(self, *, client: Any, cache_path: Path, max_price_usd: float = 10.0, cache_ttl_s: float = 6 * 3600):
        self.client = client
        self.cache_path = cache_path
        self.max_price_usd = max_price_usd
        self.cache_ttl_s = cache_ttl_s

    def _load_cache(self) -> list[InstrumentCandidate] | None:
        if not self.cache_path.exists():
            return None
        data = json.loads(self.cache_path.read_text())
        if time.time() - data["cached_at"] > self.cache_ttl_s:
            return None
        return [InstrumentCandidate(**c) for c in data["candidates"]]

    def _save_cache(self, candidates: list[InstrumentCandidate]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"cached_at": time.time(), "candidates": [c.as_dict() for c in candidates]})
        )

    async def refresh(self) -> list[InstrumentCandidate]:
        cached = self._load_cache()
        if cached is not None:
            return cached

        raw = await self.client.get(
            "/api/v1/market-data/search",
            params={
                "fields": "instrumentId,displayname,instrumentType,currentRate,isCurrentlyTradable,isDelisted",
                "instrumentType": "Stock",
                "pageSize": 500,
            },
        )
        candidates = [
            InstrumentCandidate(instrument_id=item["instrumentId"], name=item["displayname"], price=float(item["currentRate"]))
            for item in raw.get("items", [])
            if item.get("isCurrentlyTradable") and not item.get("isDelisted") and float(item["currentRate"]) <= self.max_price_usd
        ]
        log.info("etoro.universe.refreshed", size=len(candidates), max_price=self.max_price_usd)
        self._save_cache(candidates)
        return candidates
