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
    client = AsyncMock()
    client.post_order.return_value = {
        "orderId": "abc-123",
        "positionId": "pos-1",
        "status": "EXECUTED",
        "executionPrice": 4.52,
        "units": 100,
    }
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

    assert result.status == "EXECUTED"
    assert result.deal_id == "pos-1"
    assert result.fill_price == 4.52
    assert result.filled_size == 100
    client.post_order.assert_awaited_once()
    sent_payload = client.post_order.await_args.args[0]
    assert sent_payload["action"] == "open"
    assert sent_payload["transaction"] == "buy"
    assert sent_payload["instrumentId"] == 100000
    assert sent_payload["orderType"] == "mkt"
    assert sent_payload["leverage"] == 5
    assert sent_payload["units"] == 100
    assert sent_payload["stopLoss"] == 4.20
    assert sent_payload["takeProfit"] == 5.15
    assert events[0][0] == EventType.ORDER_SUBMITTED
    assert events[1][0] == EventType.ORDER_CONFIRMED


@pytest.mark.asyncio
async def test_open_market_order_rejected_emits_order_rejected() -> None:
    client = AsyncMock()
    client.post_order.return_value = {"status": "REJECTED", "reason": "insufficient funds"}
    events: list[tuple[EventType, dict]] = []

    async def fake_emit(event_type, payload, *, source="system"):
        events.append((event_type, payload))

    gateway = EtoroGateway(client=client, emit=fake_emit)
    result = await gateway.open_market_order(
        instrument_id=100000, direction=Direction.BUY, units=100, stop_loss=4.20, take_profit=5.15, leverage=5
    )

    assert result.status == "REJECTED"
    assert result.error == "insufficient funds"
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
    assert called_json == {"InstrumentId": 100000, "UnitsToDeduct": 100}
    assert events[-1][0] == EventType.POSITION_CLOSED


@pytest.mark.asyncio
async def test_positions_maps_to_broker_position_list() -> None:
    client = AsyncMock()
    client.get.return_value = {
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
    gateway = EtoroGateway(client=client, emit=AsyncMock())
    positions = await gateway.positions()

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
    client = AsyncMock()
    client.get.return_value = {"credit": 100000.0, "equity": 100250.0, "cash": 95000.0}
    gateway = EtoroGateway(client=client, emit=AsyncMock())
    account = await gateway.balances()

    assert account.currency == "USD"
    assert account.equity == 100250.0
    assert account.available == 95000.0
    assert account.balance == 100000.0
