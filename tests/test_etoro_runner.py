from __future__ import annotations

import random
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


def _candles_from_closes(closes: list[float]) -> list[DailyCandle]:
    from datetime import timedelta, timezone
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [
        DailyCandle(date=base + timedelta(days=i), open=c, high=c, low=c, close=c, volume=100_000)
        for i, c in enumerate(closes)
    ]


def _correlated_diverged_histories() -> tuple[list[DailyCandle], list[DailyCandle]]:
    # Stesso pattern di tests/test_etoro_pairs.py: trend condiviso + rumore
    # indipendente piccolo (correlazione alta ma non 1.0, varianza storica
    # reale sul rapporto), poi l'ultima seduta diverge per generare un segnale.
    rng = random.Random(7)
    a, b = [], []
    price_a, price_b = 100.0, 50.0
    for i in range(61):
        step = 1.0 if i % 2 == 0 else -1.0
        price_a += step + rng.uniform(-0.15, 0.15)
        price_b += step * 0.5 + rng.uniform(-0.08, 0.08)
        a.append(price_a)
        b.append(price_b)
    a[-1] = a[-1] * 1.15
    return _candles_from_closes(a), _candles_from_closes(b)


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
    fake_news_lookup = AsyncMock(return_value="")
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=fake_news_lookup, risk_engine=risk_engine)

    await runner.run_cycle()

    gateway.open_market_order.assert_awaited_once()
    call_kwargs = gateway.open_market_order.await_args.kwargs
    assert call_kwargs["instrument_id"] == 1
    assert call_kwargs["direction"] == Direction.BUY
    assert call_kwargs["units"] > 0
    assert call_kwargs["leverage"] == 5


@pytest.mark.asyncio
async def test_run_cycle_does_not_rejudge_same_instrument_when_news_unchanged() -> None:
    # Le candele giornaliere non cambiano infra-day: senza dedup lo stesso
    # titolo verrebbe ri-giudicato dall'LLM ad ogni ciclo per ore (visto in
    # produzione 28/8, Ackermans & Van Haaren giudicato 9+ volte in ~90 min).
    # Il dedup e' sul CONTENUTO delle notizie, non su un timer: se non cambia
    # nulla non c'e' motivo di ripagare il giudizio LLM per lo stesso esito.
    universe = AsyncMock()
    universe.refresh.return_value = [InstrumentCandidate(instrument_id=1, name="PennyCo", price=3.55)]
    candles = AsyncMock()
    candles.daily_candles.return_value = _flat_history_with_spike()
    rates = AsyncMock()
    rates.quotes_for.return_value = [Quote(epic="ETORO:1", bid=3.53, offer=3.55, source="etoro-rest", market_status=MarketStatus.TRADEABLE)]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []

    fake_judge = AsyncMock(return_value=CatalystVerdict(has_catalyst=False, rationale="no catalyst"))
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=AsyncMock(return_value=""))

    await runner.run_cycle()
    await runner.run_cycle()

    fake_judge.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cycle_rejudges_immediately_when_news_changes() -> None:
    # Bug reale corretto in produzione 28/8: un cooldown a TEMPO avrebbe tenuto
    # bloccato un titolo (AECOM) anche dopo l'arrivo di una notizia vera e
    # specifica, solo perche' non era passata un'ora dall'ultimo giudizio.
    universe = AsyncMock()
    universe.refresh.return_value = [InstrumentCandidate(instrument_id=1, name="PennyCo", price=3.55)]
    candles = AsyncMock()
    candles.daily_candles.return_value = _flat_history_with_spike()
    rates = AsyncMock()
    rates.quotes_for.return_value = [Quote(epic="ETORO:1", bid=3.53, offer=3.55, source="etoro-rest", market_status=MarketStatus.TRADEABLE)]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []

    fake_judge = AsyncMock(return_value=CatalystVerdict(has_catalyst=False, rationale="no catalyst"))
    fake_news = AsyncMock(side_effect=["", "- PennyCo wins FDA approval (Reuters, now)"])
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=fake_news)

    await runner.run_cycle()
    await runner.run_cycle()

    assert fake_judge.await_count == 2


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

    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=AsyncMock(return_value=""))

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


@pytest.mark.asyncio
async def test_run_cycle_opens_both_legs_of_approved_pair_trade() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="CorrA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="CorrB", price=50.0)
    hist_a, hist_b = _correlated_diverged_histories()

    universe = AsyncMock()
    universe.refresh.return_value = [inst_a, inst_b]
    candles = AsyncMock()
    candles.daily_candles.side_effect = lambda *, instrument_id, count: hist_a if instrument_id == 1 else hist_b
    rates = AsyncMock()
    rates.quotes_for.return_value = [
        Quote(epic="ETORO:1", bid=hist_a[-1].close - 0.05, offer=hist_a[-1].close, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
        Quote(epic="ETORO:2", bid=hist_b[-1].close - 0.05, offer=hist_b[-1].close, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
    ]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []

    fake_judge = AsyncMock(return_value=CatalystVerdict(has_catalyst=False, rationale="no catalyst"))
    risk_engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=AsyncMock(return_value=""), risk_engine=risk_engine)

    await runner.run_cycle()

    assert gateway.open_market_order.await_count == 2
    calls = gateway.open_market_order.await_args_list
    instrument_ids = {c.kwargs["instrument_id"] for c in calls}
    assert instrument_ids == {1, 2}
    directions = {c.kwargs["instrument_id"]: c.kwargs["direction"] for c in calls}
    # Le due gambe hanno sempre direzione opposta (market-neutral).
    assert directions[1] != directions[2]


@pytest.mark.asyncio
async def test_run_cycle_evaluates_pairs_even_when_no_momentum_candidates() -> None:
    # Bug corretto: un return anticipato quando momentum e' vuoto impediva del
    # tutto la valutazione dei pairs, strategia indipendente che non deve
    # dipendere da un salto di prezzo momentum.
    inst_a = InstrumentCandidate(instrument_id=1, name="CorrA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="CorrB", price=50.0)
    hist_a, hist_b = _correlated_diverged_histories()
    # A si muove solo +2.5% (sotto la soglia gap momentum del 3%), B resta
    # fermo sull'ultima seduta: il gap giornaliero di A da solo non basta per
    # momentum, ma il RAPPORTO storico A/B diverge abbastanza per un segnale pairs.
    last_a = hist_a[-2].close * 1.025
    hist_a_below_momentum = hist_a[:-1] + [DailyCandle(date=hist_a[-1].date, open=last_a, high=last_a, low=last_a, close=last_a, volume=100_000)]
    hist_b_flat_last_day = hist_b[:-1] + [DailyCandle(date=hist_b[-1].date, open=hist_b[-2].close, high=hist_b[-2].close, low=hist_b[-2].close, close=hist_b[-2].close, volume=100_000)]

    universe = AsyncMock()
    universe.refresh.return_value = [inst_a, inst_b]
    candles = AsyncMock()
    candles.daily_candles.side_effect = lambda *, instrument_id, count: hist_a_below_momentum if instrument_id == 1 else hist_b_flat_last_day
    rates = AsyncMock()
    rates.quotes_for.return_value = [
        Quote(epic="ETORO:1", bid=last_a - 0.05, offer=last_a, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
        Quote(epic="ETORO:2", bid=hist_b_flat_last_day[-1].close - 0.05, offer=hist_b_flat_last_day[-1].close, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
    ]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    gateway.positions.return_value = []

    fake_judge = AsyncMock()  # non deve MAI essere chiamato: gap 2.5% < soglia momentum 3%
    risk_engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=AsyncMock(return_value=""), risk_engine=risk_engine)

    await runner.run_cycle()

    fake_judge.assert_not_awaited()  # conferma: zero candidati momentum in questo ciclo
    assert gateway.open_market_order.await_count == 2  # ma i pairs hanno comunque aperto una coppia


@pytest.mark.asyncio
async def test_run_cycle_skips_pairs_when_not_enough_free_position_slots() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="CorrA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="CorrB", price=50.0)
    hist_a, hist_b = _correlated_diverged_histories()

    universe = AsyncMock()
    universe.refresh.return_value = [inst_a, inst_b]
    candles = AsyncMock()
    candles.daily_candles.side_effect = lambda *, instrument_id, count: hist_a if instrument_id == 1 else hist_b
    rates = AsyncMock()
    rates.quotes_for.return_value = [
        Quote(epic="ETORO:1", bid=hist_a[-1].close - 0.05, offer=hist_a[-1].close, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
        Quote(epic="ETORO:2", bid=hist_b[-1].close - 0.05, offer=hist_b[-1].close, source="etoro-rest", market_status=MarketStatus.TRADEABLE),
    ]
    gateway = AsyncMock()
    gateway.balances.return_value = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    # MAX_OPEN_POSITIONS=3: con 2 gia' aperte resta 1 solo slot, non bastano le 2
    # gambe di una coppia.
    gateway.positions.return_value = [
        BrokerPosition(deal_id="pos-1", epic="ETORO:9", direction=Direction.BUY, size=10, level=5.0, currency="USD"),
        BrokerPosition(deal_id="pos-2", epic="ETORO:10", direction=Direction.BUY, size=10, level=5.0, currency="USD"),
    ]

    fake_judge = AsyncMock(return_value=CatalystVerdict(has_catalyst=False, rationale="no catalyst"))
    risk_engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=AsyncMock(), judge_fn=fake_judge, news_lookup_fn=AsyncMock(return_value=""), risk_engine=risk_engine)

    await runner.run_cycle()

    gateway.open_market_order.assert_not_awaited()
