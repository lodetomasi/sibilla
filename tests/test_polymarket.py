"""Test collector Polymarket con HTTP mockato (sez. 79: API mocks)."""
from __future__ import annotations

import httpx
import pytest

from collectors.base import CollectionMode
from collectors.polymarket.client import PolymarketClient
from collectors.polymarket.markets import PolymarketMarketCollector
from collectors.polymarket.parsers import parse_market, parse_wallet_trade
from collectors.polymarket.wallets import PolymarketWalletCollector
from collectors.polymarket.ws import PolymarketWebSocket
from core.enums import EventType
from core.http import HttpClient
from core.repository import Repository

MARKET_RAW = {
    "id": "512345",
    "conditionId": "0xcond1",
    "slug": "arsenal-chelsea",
    "question": "Will Arsenal win against Chelsea?",
    "outcomes": '["Yes","No"]',
    "outcomePrices": '["0.62","0.38"]',
    "clobTokenIds": '["111","222"]',
    "volumeNum": 154000.5,
    "volume24hr": 5000.0,
    "liquidityNum": 22000.0,
    "endDate": "2026-09-01T18:00:00Z",
    "bestBid": 0.61,
    "bestAsk": 0.63,
    "spread": 0.02,
    "active": True,
    "closed": False,
    "acceptingOrders": True,
    "tags": [{"slug": "sports"}],
    "events": [{"slug": "epl-arsenal-chelsea", "title": "Arsenal vs Chelsea"}],
}

BOOK_RAW = {
    "market": "0xcond1",
    "asset_id": "111",
    "bids": [{"price": "0.60", "size": "500"}, {"price": "0.59", "size": "300"}],
    "asks": [{"price": "0.63", "size": "200"}, {"price": "0.64", "size": "100"}],
}

TRADE_RAW = {
    "proxyWallet": "0xWALLET1",
    "price": 0.6,
    "size": 100,
    "side": "BUY",
    "conditionId": "0xcond1",
    "asset": "111",
    "title": "Will Arsenal win against Chelsea?",
    "timestamp": 1787000000,
    "transactionHash": "0xhash1",
    "outcome": "Yes",
}

POSITION_RAW = {
    "proxyWallet": "0xWALLET1",
    "asset": "111",
    "conditionId": "0xcond1",
    "title": "Will Arsenal win against Chelsea?",
    "outcome": "Yes",
    "size": 100,
    "avgPrice": 0.6,
    "curPrice": 0.65,
    "cashPnl": 5.0,
    "realizedPnl": 1.0,
}

HOLDERS_RAW = {"holders": [{"proxyWallet": "0xWALLET1", "amount": 100}, {"proxyWallet": "0xWALLET2", "amount": 50}]}


def make_client() -> PolymarketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/markets":
            return httpx.Response(200, json=[MARKET_RAW])
        if path.startswith("/markets/"):
            return httpx.Response(200, json=MARKET_RAW)
        if path == "/books":
            return httpx.Response(200, json=[BOOK_RAW, {**BOOK_RAW, "asset_id": "222"}])
        if path == "/book":
            return httpx.Response(200, json=BOOK_RAW)
        if path == "/prices-history":
            return httpx.Response(
                200, json={"history": [{"t": 1786990000, "p": 0.58}, {"t": 1786993600, "p": 0.60}]}
            )
        if path == "/trades":
            return httpx.Response(200, json=[TRADE_RAW])
        if path == "/positions":
            return httpx.Response(200, json=[POSITION_RAW])
        if path == "/holders":
            return httpx.Response(200, json=HOLDERS_RAW)
        if path in ("/leaderboard", "/rankings"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    return PolymarketClient(
        gamma=HttpClient(provider="gamma", rps=1000, client=httpx.AsyncClient(transport=transport, base_url="https://gamma")),
        clob=HttpClient(provider="clob", rps=1000, client=httpx.AsyncClient(transport=transport, base_url="https://clob")),
        data=HttpClient(provider="data", rps=1000, client=httpx.AsyncClient(transport=transport, base_url="https://data")),
    )


def test_parse_market_normalizza_json_stringa():
    parsed = parse_market(MARKET_RAW)
    assert parsed["external_id"] == "0xcond1"
    assert parsed["category"] == "sports"
    assert parsed["tradable"] is False  # Polymarket = intelligence source (sez. 4.1)
    assert [o["name"] for o in parsed["outcomes"]] == ["Yes", "No"]
    assert parsed["outcomes"][0]["token_id"] == "111"
    assert parsed["volume"] == 154000.5


def test_parse_wallet_trade_calcola_usd():
    parsed = parse_wallet_trade(TRADE_RAW)
    assert parsed["wallet_address"] == "0xwallet1"
    assert parsed["usd_size"] == pytest.approx(60.0)
    assert parsed["category"] == "sports"


async def test_client_endpoints():
    client = make_client()
    markets = await client.list_markets(limit=1)
    assert markets[0]["conditionId"] == "0xcond1"
    books = await client.get_books(["111", "222"])
    assert len(books) == 2
    history = await client.price_history("111")
    assert len(history) == 2
    positions = await client.wallet_positions("0xwallet1")
    assert positions[0]["size"] == 100
    holders = await client.market_holders("0xcond1")
    assert len(holders) == 2
    assert await client.leaderboard() == []  # endpoint assente -> lista vuota, non errore
    await client.aclose()


async def test_market_collector_incremental(engine, bus, memory_cache):
    collector = PolymarketMarketCollector(make_client(), book_top_n=5)
    stored = await collector.run_once(CollectionMode.INCREMENTAL, limit=10)
    assert stored == 1

    from core.db import session_scope

    async with session_scope() as session:
        repo = Repository(session)
        market = await repo.get_market("polymarket", "0xcond1")
        assert market is not None
        assert market.category == "sports"
        assert market.event_id is not None
        prices = await repo.price_history(market.id, limit=10)
        assert prices, "deve salvare almeno uno snapshot di prezzo"
        book = await repo.latest_orderbook(market.id, outcome="Yes")
        assert book is not None
        assert book.features["order_book_imbalance"] == pytest.approx((500 - 200) / 700)

    price_events = bus.events_of(EventType.PRICE_CHANGED)
    assert price_events, "il collector deve emettere PRICE_CHANGED"
    await collector.aclose()


async def test_market_collector_batch_con_storico(engine, bus, memory_cache):
    collector = PolymarketMarketCollector(make_client())
    await collector.run_once(
        CollectionMode.HISTORICAL_BATCH, max_pages=1, with_history=True, history_top_n=1
    )
    from core.db import session_scope

    async with session_scope() as session:
        repo = Repository(session)
        market = await repo.get_market("polymarket", "0xcond1")
        history = await repo.price_history(market.id, limit=50)
        assert any(row.source == "history" for row in history)
    await collector.aclose()


async def test_wallet_collector_raccoglie_trade_e_posizioni(engine, bus, memory_cache):
    market_collector = PolymarketMarketCollector(make_client(), book_top_n=1)
    await market_collector.run_once(CollectionMode.INCREMENTAL, limit=5)

    collector = PolymarketWalletCollector(make_client())
    addresses = await collector.discover(markets_limit=5, holders_per_market=10, use_leaderboard=True)
    assert "0xwallet1" in addresses

    inserted = await collector.collect_wallet("0xwallet1", full_history=True)
    assert inserted == 1

    # idempotenza: la seconda raccolta non duplica
    assert await collector.collect_wallet("0xwallet1") == 0

    from core.db import session_scope

    async with session_scope() as session:
        repo = Repository(session)
        trades = await repo.wallet_trades("0xwallet1")
        assert len(trades) == 1
        positions = await repo.wallet_positions("0xwallet1")
        assert positions[0].current_price == 0.65

    await collector.refresh_trade_counters()
    async with session_scope() as session:
        repo = Repository(session)
        wallet = await repo.get_wallet("0xwallet1")
        assert wallet.n_trades == 1
        assert wallet.total_volume == pytest.approx(60.0)

    assert bus.events_of(EventType.WALLET_TRADE)
    await collector.aclose()
    await market_collector.aclose()


async def test_websocket_applica_book_e_price_change(bus, memory_cache):
    ws = PolymarketWebSocket(["111"])
    await ws.handle_message(
        {
            "event_type": "book",
            "asset_id": "111",
            "market": "0xcond1",
            "bids": [{"price": "0.60", "size": "100"}],
            "asks": [{"price": "0.62", "size": "80"}],
        }
    )
    assert ws.books["111"].mid == pytest.approx(0.61)
    await ws.handle_message(
        {
            "event_type": "price_change",
            "asset_id": "111",
            "changes": [{"price": "0.61", "size": "50", "side": "BUY"}],
        }
    )
    assert ws.books["111"].best_bid == pytest.approx(0.61)
    events = bus.events_of(EventType.PRICE_CHANGED)
    assert len(events) == 2
    cached = await memory_cache.get_json("price:polymarket:111")
    assert cached["price"] == pytest.approx(0.615)
