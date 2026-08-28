"""Prezzi live (rates) e storico candele (candle history) eToro."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.enums import MarketStatus
from core.schemas import Quote
from execution.etoro.gateway import etoro_epic

MAX_IDS_PER_RATES_CALL = 100


@dataclass
class DailyCandle:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class RatesCollector:
    def __init__(self, *, client: Any):
        self.client = client

    async def quotes_for(self, instrument_ids: list[int]) -> list[Quote]:
        out: list[Quote] = []
        for i in range(0, len(instrument_ids), MAX_IDS_PER_RATES_CALL):
            chunk = instrument_ids[i : i + MAX_IDS_PER_RATES_CALL]
            raw = await self.client.get(
                "/api/v1/market-data/instruments/rates",
                params={"instrumentIds": ",".join(str(x) for x in chunk)},
            )
            for r in raw.get("rates", []):
                # La risposta reale usa "instrumentID" (ID maiuscolo), non "instrumentId"
                # come altri endpoint eToro (es. search, pnl) - verificato in produzione 28/8.
                instrument_id = r.get("instrumentID", r.get("instrumentId"))
                if instrument_id is None:
                    continue
                out.append(
                    Quote(
                        epic=etoro_epic(int(instrument_id)),
                        bid=float(r["bid"]),
                        offer=float(r["ask"]),
                        market_status=MarketStatus.TRADEABLE,
                        source="etoro-rest",
                        raw=r,
                    )
                )
        return out


class CandleHistory:
    def __init__(self, *, client: Any):
        self.client = client

    async def daily_candles(self, *, instrument_id: int, count: int) -> list[DailyCandle]:
        raw = await self.client.get(
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/OneDay/{count}"
        )
        blocks = raw.get("candles", [])
        if not blocks:
            return []
        series = blocks[0].get("candles", [])
        return [
            DailyCandle(
                date=datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")),
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c.get("volume") or 0.0),
            )
            for c in series
        ]
