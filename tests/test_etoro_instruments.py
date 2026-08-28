from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from collectors.etoro.instruments import InstrumentUniverse

SAMPLE_PAGE = {
    "page": 1,
    "pageSize": 100,
    "totalItems": 3,
    "items": [
        {"instrumentId": 1, "displayname": "PennyCo", "instrumentType": "Stock", "currentRate": 3.20, "isCurrentlyTradable": True, "isDelisted": False},
        {"instrumentId": 2, "displayname": "TooExpensive", "instrumentType": "Stock", "currentRate": 55.0, "isCurrentlyTradable": True, "isDelisted": False},
        {"instrumentId": 3, "displayname": "Delisted", "instrumentType": "Stock", "currentRate": 1.10, "isCurrentlyTradable": False, "isDelisted": True},
    ],
}


@pytest.mark.asyncio
async def test_refresh_filters_price_and_tradability(tmp_path: Path) -> None:
    client = AsyncMock()
    client.get.return_value = SAMPLE_PAGE
    universe = InstrumentUniverse(client=client, cache_path=tmp_path / "etoro_universe.json", max_price_usd=10.0)

    candidates = await universe.refresh()

    assert [c.instrument_id for c in candidates] == [1]
    assert candidates[0].price == 3.20
    client.get.assert_awaited_once()
    call_kwargs = client.get.await_args.kwargs
    assert call_kwargs["params"]["instrumentType"] == "Stock"


@pytest.mark.asyncio
async def test_refresh_writes_cache_file(tmp_path: Path) -> None:
    client = AsyncMock()
    client.get.return_value = SAMPLE_PAGE
    cache_path = tmp_path / "etoro_universe.json"
    universe = InstrumentUniverse(client=client, cache_path=cache_path, max_price_usd=10.0)

    await universe.refresh()

    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["candidates"][0]["instrument_id"] == 1
    assert "cached_at" in data


@pytest.mark.asyncio
async def test_refresh_uses_cache_within_ttl(tmp_path: Path) -> None:
    client = AsyncMock()
    client.get.return_value = SAMPLE_PAGE
    cache_path = tmp_path / "etoro_universe.json"
    universe = InstrumentUniverse(client=client, cache_path=cache_path, max_price_usd=10.0, cache_ttl_s=21600)

    await universe.refresh()
    await universe.refresh()

    assert client.get.await_count == 1  # secondo refresh serve dalla cache
