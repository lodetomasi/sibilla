from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from collectors.etoro.instruments import PAGE_SIZE, TARGET_UNIVERSE_SIZE, InstrumentUniverse

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


@pytest.mark.asyncio
async def test_refresh_paginates_when_first_page_has_few_tradable(tmp_path: Path) -> None:
    # Osservato in produzione (28/8): la ricerca ordina per instrumentId crescente,
    # non per rilevanza - una pagina piena (len(items) == PAGE_SIZE) di soli titoli
    # non tradabili deve far proseguire alla pagina successiva, non fermarsi.
    full_untradable_page = {
        "page": 1, "pageSize": PAGE_SIZE, "totalItems": PAGE_SIZE + 1,
        "items": [
            {"instrumentId": i, "displayname": f"Old{i}", "instrumentType": "Stock", "currentRate": 5.0, "isCurrentlyTradable": False, "isDelisted": False}
            for i in range(PAGE_SIZE)
        ],
    }
    second_page = {
        "page": 2, "pageSize": PAGE_SIZE, "totalItems": PAGE_SIZE + 1,
        "items": [
            {"instrumentId": 999999, "displayname": "Fresh", "instrumentType": "Stock", "currentRate": 5.0, "isCurrentlyTradable": True, "isDelisted": False},
        ],
    }
    client = AsyncMock()
    client.get.side_effect = [full_untradable_page, second_page]
    universe = InstrumentUniverse(client=client, cache_path=tmp_path / "etoro_universe.json", max_price_usd=10.0)

    candidates = await universe.refresh()

    assert client.get.await_count == 2
    assert [c.instrument_id for c in candidates] == [999999]
    second_call_params = client.get.await_args_list[1].kwargs["params"]
    assert second_call_params["page"] == 2


@pytest.mark.asyncio
async def test_refresh_caps_universe_at_target_size(tmp_path: Path) -> None:
    big_page = {
        "page": 1, "pageSize": PAGE_SIZE, "totalItems": PAGE_SIZE,
        "items": [
            {"instrumentId": i, "displayname": f"Co{i}", "instrumentType": "Stock", "currentRate": 5.0, "isCurrentlyTradable": True, "isDelisted": False}
            for i in range(PAGE_SIZE)
        ],
    }
    client = AsyncMock()
    client.get.return_value = big_page
    universe = InstrumentUniverse(client=client, cache_path=tmp_path / "etoro_universe.json", max_price_usd=10.0)

    candidates = await universe.refresh()

    assert len(candidates) == TARGET_UNIVERSE_SIZE
    client.get.assert_awaited_once()  # basta la prima pagina per superare il target


@pytest.mark.asyncio
async def test_refresh_skips_malformed_item_without_current_rate(tmp_path: Path) -> None:
    # Visto in produzione: l'item {"instrumentId": -100000} arriva senza altri campi.
    page = {
        "page": 1, "pageSize": PAGE_SIZE, "totalItems": 2,
        "items": [
            {"instrumentId": -100000},
            {"instrumentId": 1, "displayname": "PennyCo", "instrumentType": "Stock", "currentRate": 3.20, "isCurrentlyTradable": True, "isDelisted": False},
        ],
    }
    client = AsyncMock()
    client.get.return_value = page
    universe = InstrumentUniverse(client=client, cache_path=tmp_path / "etoro_universe.json", max_price_usd=10.0)

    candidates = await universe.refresh()

    assert [c.instrument_id for c in candidates] == [1]
