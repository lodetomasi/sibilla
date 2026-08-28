from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.enums import Direction, EventType
from execution.etoro.gateway import EtoroGateway, etoro_epic, instrument_id_from_epic


def test_epic_roundtrip() -> None:
    assert etoro_epic(100000) == "ETORO:100000"
    assert instrument_id_from_epic("ETORO:100000") == 100000


@pytest.mark.asyncio
async def test_open_market_order_maps_response_to_order_result() -> None:
    # Risposta reale di POST create-an-order (verificata via doc ufficiale 28/8):
    # SOLO {token, orderId, referenceId} - nessun status/positionId/executionPrice
    # sincrono, il fill si scopre al prossimo poll di positions().
    client = AsyncMock()
    client.post_order.return_value = {"token": "tok-1", "orderId": 999, "referenceId": "ref-1"}
    events: list[tuple[EventType, dict]] = []

    async def fake_emit(event_type, payload, *, source="system"):
        events.append((event_type, payload))

    gateway = EtoroGateway(client=client, emit=fake_emit)
    result = await gateway.open_market_order(
        instrument_id=100000,
        direction=Direction.BUY,
        units=100,
        stop_loss=4.20,
        take_profit=5.15,
        leverage=5,
    )

    assert result.status == "CONFIRMED"
    assert result.client_order_id == "999"
    assert result.deal_id is None
    client.post_order.assert_awaited_once()
    sent_payload = client.post_order.await_args.args[0]
    assert sent_payload["action"] == "open"
    assert sent_payload["transaction"] == "buy"
    assert sent_payload["instrumentId"] == 100000
    assert sent_payload["orderType"] == "mkt"
    assert sent_payload["settlementType"] == "cfd"
    assert sent_payload["leverage"] == 5
    assert sent_payload["units"] == 100
    assert sent_payload["stopLossRate"] == 4.20
    assert sent_payload["takeProfitRate"] == 5.15
    assert events[0][0] == EventType.ORDER_SUBMITTED
    assert events[1][0] == EventType.ORDER_CONFIRMED


@pytest.mark.asyncio
async def test_open_market_order_rejected_emits_order_rejected() -> None:
    # Nessun orderId nella risposta = il broker non ha accettato la richiesta.
    client = AsyncMock()
    client.post_order.return_value = {"errorCode": "InsufficientFunds"}
    events: list[tuple[EventType, dict]] = []

    async def fake_emit(event_type, payload, *, source="system"):
        events.append((event_type, payload))

    gateway = EtoroGateway(client=client, emit=fake_emit)
    result = await gateway.open_market_order(
        instrument_id=100000, direction=Direction.BUY, units=100, stop_loss=4.20, take_profit=5.15, leverage=5
    )

    assert result.status == "REJECTED"
    assert "InsufficientFunds" in result.error
    assert events[-1][0] == EventType.ORDER_REJECTED


@pytest.mark.asyncio
async def test_open_market_order_requires_explicit_leverage() -> None:
    # Iron rule: la leva NON ha un default silenzioso nel gateway (design doc: "leva
    # passata esplicitamente dal chiamante") — decisa dal risk adapter (Task 9), mai
    # dal layer di esecuzione.
    client = AsyncMock()
    gateway = EtoroGateway(client=client, emit=AsyncMock())
    with pytest.raises(TypeError):
        await gateway.open_market_order(instrument_id=100000, direction=Direction.BUY, units=100, stop_loss=4.20, take_profit=5.15)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_close_position_emits_position_closed() -> None:
    client = AsyncMock()
    client.settings.execution_mode.uses_real_money = False
    client.post.return_value = {
        "orderForClose": {"positionID": 2150941015, "instrumentID": 100000, "unitsToDeduct": 100}
    }
    events: list[tuple[EventType, dict]] = []

    async def fake_emit(event_type, payload, *, source="system"):
        events.append((event_type, payload))

    gateway = EtoroGateway(client=client, emit=fake_emit)
    await gateway.close_position(position_id="2150941015", instrument_id=100000, units=100)

    client.post.assert_awaited_once()
    called_path = client.post.await_args.args[0]
    assert called_path == "/api/v1/trading/execution/demo/market-close-orders/positions/2150941015"
    called_json = client.post.await_args.kwargs["json"]
    assert called_json == {"InstrumentID": 100000, "UnitsToDeduct": 100}
    assert events[-1][0] == EventType.POSITION_CLOSED


@pytest.mark.asyncio
async def test_positions_maps_to_broker_position_list() -> None:
    client = AsyncMock()
    client.settings.execution_mode.uses_real_money = False
    client.get.return_value = {
        "clientPortfolio": {
            "positions": [
                {
                    "positionId": "pos-1",
                    "instrumentId": 100000,
                    "isBuy": True,
                    "units": 100,
                    "openRate": 4.30,
                    "stopLossRate": 4.00,
                    "takeProfitRate": 5.00,
                }
            ]
        }
    }
    gateway = EtoroGateway(client=client, emit=AsyncMock())
    positions = await gateway.positions()

    client.get.assert_awaited_once_with("/api/v1/trading/info/demo/pnl")
    assert len(positions) == 1
    p = positions[0]
    assert p.deal_id == "pos-1"
    assert p.epic == "ETORO:100000"
    assert p.direction == Direction.BUY
    assert p.size == 100
    assert p.level == 4.30
    assert p.stop_level == 4.00
    assert p.limit_level == 5.00
    assert p.currency == "USD"


@pytest.mark.asyncio
async def test_balances_maps_to_account_state() -> None:
    # /api/v1/balances* torna vuoto sul conto demo (verificato in produzione 28/8): il
    # credito virtuale vive in clientPortfolio dello stesso endpoint di positions().
    client = AsyncMock()
    client.settings.execution_mode.uses_real_money = False
    client.get.return_value = {
        "clientPortfolio": {"credit": 100000.0, "unrealizedPnL": 250.0, "positions": []}
    }
    gateway = EtoroGateway(client=client, emit=AsyncMock())
    account = await gateway.balances()

    client.get.assert_awaited_once_with("/api/v1/trading/info/demo/pnl")
    assert account.currency == "USD"
    assert account.equity == 100250.0
    assert account.available == 100000.0
    assert account.balance == 100000.0
