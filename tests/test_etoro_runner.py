from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle
from core.config import RiskLimits
from core.enums import Direction, MarketStatus
from core.schemas import AccountState, BrokerPosition, Quote, RiskDecision
from intelligence.etoro_judge import CatalystVerdict
from risk.engine import RiskEngine
from workers.etoro_runner import EtoroRunner

NY = ZoneInfo("America/New_York")


def _flat_history_with_spike() -> list[DailyCandle]:
    from datetime import timezone
    out = []
    for i in range(20):
        out.append(DailyCandle(date=datetime(2026, 8, i + 1, tzinfo=timezone.utc), open=3.0, high=3.0, low=3.0, close=3.0, volume=100_000))
    out.append(DailyCandle(date=datetime(2026, 8, 21, tzinfo=timezone.utc), open=3.0, high=3.6, low=2.95, close=3.55, volume=900_000))
    return out


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 8, 28, 10, 0, tzinfo=NY), True),   # venerdi' 10:00 NY -> aperto
        (datetime(2026, 8, 28, 8, 0, tzinfo=NY), False),   # prima apertura
        (datetime(2026, 8, 28, 17, 0, tzinfo=NY), False),  # dopo chiusura
        (datetime(2026, 8, 29, 10, 0, tzinfo=NY), False),  # sabato
    ],
)
def test_is_market_open(when: datetime, expected: bool) -> None:
    runner = EtoroRunner(universe=AsyncMock(), rates=AsyncMock(), candles=AsyncMock(), gateway=AsyncMock(), llm=AsyncMock())
    assert runner.is_market_open(when) is expected


@pytest.mark.asyncio
async def test_run_cycle_opens_order_on_approved_catalyst_trade() -> None:
    universe = AsyncMock()
    universe.refresh.return_value = [InstrumentCandidate(instrument_id=1, name="PennyCo", price=3.55)]
    candles = AsyncMock()
    candles.daily_candles.return_value = _flat_history_with_spike()
    rates = AsyncMock()
    rates.quotes_for.return_value = [Quote(epic="ETORO:1", bid=3.53, offer=3.55, source="etoro-rest", market_status=MarketStatus.TRADEABLE)]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []
    gateway.open_market_order.return_value = None

    async def fake_judge(candidate, *, news_brief, llm):
        return CatalystVerdict(has_catalyst=True, direction="BUY", confidence=0.7, rationale="FDA news")

    # max_holding_time_s di default (4h) e' calibrato per il vecchio motore Limitless;
    # questo motore tiene fino al time-stop EOD (~6h30, Task 11 alza il limite via
    # ATS_MAX_HOLDING_TIME_S in produzione) - qui si riflette la stessa config.
    risk_engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, risk_engine=risk_engine)

    await runner.run_cycle()

    gateway.open_market_order.assert_awaited_once()
    call_kwargs = gateway.open_market_order.await_args.kwargs
    assert call_kwargs["instrument_id"] == 1
    assert call_kwargs["direction"] == Direction.BUY
    assert call_kwargs["units"] > 0
    assert call_kwargs["leverage"] == 5


@pytest.mark.asyncio
async def test_run_cycle_skips_order_without_catalyst() -> None:
    universe = AsyncMock()
    universe.refresh.return_value = [InstrumentCandidate(instrument_id=1, name="PennyCo", price=3.55)]
    candles = AsyncMock()
    candles.daily_candles.return_value = _flat_history_with_spike()
    rates = AsyncMock()
    rates.quotes_for.return_value = [Quote(epic="ETORO:1", bid=3.53, offer=3.55, source="etoro-rest", market_status=MarketStatus.TRADEABLE)]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []

    async def fake_judge(candidate, *, news_brief, llm):
        return CatalystVerdict(has_catalyst=False, rationale="no verifiable cause")

    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge)

    await runner.run_cycle()

    gateway.open_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_cycle_skips_when_position_cap_reached() -> None:
    universe = AsyncMock()
    gateway = AsyncMock()
    gateway.positions.return_value = [
        BrokerPosition(deal_id=f"pos-{i}", epic=f"ETORO:{i}", direction=Direction.BUY, size=10, level=1.0, currency="USD")
        for i in range(3)
    ]
    runner = EtoroRunner(universe=universe, rates=AsyncMock(), candles=AsyncMock(), gateway=gateway, llm=AsyncMock())

    await runner.run_cycle()

    universe.refresh.assert_not_awaited()
    gateway.open_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_time_stop_closes_all_open_positions() -> None:
    gateway = AsyncMock()
    gateway.positions.return_value = [
        BrokerPosition(deal_id="pos-1", epic="ETORO:1", direction=Direction.BUY, size=100, level=3.55, currency="USD"),
        BrokerPosition(deal_id="pos-2", epic="ETORO:2", direction=Direction.BUY, size=50, level=1.20, currency="USD"),
    ]
    runner = EtoroRunner(universe=AsyncMock(), rates=AsyncMock(), candles=AsyncMock(), gateway=gateway, llm=AsyncMock())

    await runner.time_stop_close_all()

    assert gateway.close_position.await_count == 2
    first_call = gateway.close_position.await_args_list[0].kwargs
    assert first_call["position_id"] == "pos-1"
    assert first_call["instrument_id"] == 1
    assert first_call["units"] == 100
