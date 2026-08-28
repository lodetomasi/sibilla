from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from collectors.etoro.rates import CandleHistory, RatesCollector

RATES_RESPONSE = {
    "rates": [
        {"instrumentId": 1, "bid": 3.18, "ask": 3.22, "lastExecution": 3.20, "date": "2026-08-28T14:00:00Z"},
    ]
}

CANDLES_RESPONSE = {
    "candles": [
        {
            "instrumentId": 1,
            "candles": [
                {"instrumentID": 1, "fromDate": "2026-08-26T00:00:00Z", "open": 2.90, "high": 3.05, "low": 2.85, "close": 3.00, "volume": 120000},
                {"instrumentID": 1, "fromDate": "2026-08-27T00:00:00Z", "open": 3.00, "high": 3.60, "low": 2.95, "close": 3.55, "volume": 900000},
            ],
        }
    ]
}


@pytest.mark.asyncio
async def test_quotes_for_maps_bid_ask_to_quote_schema() -> None:
    client = AsyncMock()
    client.get.return_value = RATES_RESPONSE
    collector = RatesCollector(client=client)

    quotes = await collector.quotes_for([1])

    assert quotes[0].epic == "ETORO:1"
    assert quotes[0].bid == 3.18
    assert quotes[0].offer == 3.22
    assert quotes[0].source == "etoro-rest"


@pytest.mark.asyncio
async def test_quotes_for_chunks_over_100_instruments() -> None:
    client = AsyncMock()
    client.get.return_value = {"rates": []}
    collector = RatesCollector(client=client)
    ids = list(range(1, 251))  # 250 id -> 3 chiamate (100+100+50)

    await collector.quotes_for(ids)

    assert client.get.await_count == 3
    first_call_params = client.get.await_args_list[0].kwargs["params"]
    assert first_call_params["instrumentIds"].count(",") == 99


@pytest.mark.asyncio
async def test_daily_candles_returns_close_and_volume_series() -> None:
    client = AsyncMock()
    client.get.return_value = CANDLES_RESPONSE
    history = CandleHistory(client=client)

    candles = await history.daily_candles(instrument_id=1, count=2)

    assert len(candles) == 2
    assert candles[-1].close == 3.55
    assert candles[-1].volume == 900000
    assert candles[0].close == 3.00


@pytest.mark.asyncio
async def test_daily_candles_defaults_null_volume_to_zero() -> None:
    # Visto in produzione: alcune candele reali eToro arrivano con volume=null
    # (es. strumenti a bassa liquidita'/pre-market) -> float(None) crashava il ciclo.
    client = AsyncMock()
    client.get.return_value = {
        "candles": [
            {
                "instrumentId": 1,
                "candles": [
                    {"fromDate": "2026-08-26T00:00:00Z", "open": 2.90, "high": 3.05, "low": 2.85, "close": 3.00, "volume": None},
                ],
            }
        ]
    }
    history = CandleHistory(client=client)

    candles = await history.daily_candles(instrument_id=1, count=1)

    assert candles[0].volume == 0.0
