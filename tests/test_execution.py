"""Paper broker, execution engine (PAPER/SHADOW), monitor, riconciliazione IG mockata."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from core.clock import utcnow
from core.config import load_settings
from core.enums import (
    AssetClass,
    Direction,
    ExecutionMode,
    ExitReason,
    MarketStatus,
    OrderStatus,
    PositionStatus,
    ReasonCode,
    SignalType,
)
from core.repository import Repository
from core.schemas import CostEstimate, Instrument, OrderRequest, Quote, ResidualAlpha, TradeProposal
from execution.engine import ExecutionEngine
from execution.monitor import PositionMonitor
from execution.paper import PaperBroker
from market.instrument_registry import InstrumentRegistry
from market.prices import PriceService
from risk.kill_switch import KillSwitch

EPIC = "IX.D.NASDAQ.IFE.IP"


def nasdaq() -> Instrument:
    return Instrument(epic=EPIC, name="US Tech 100", asset_class=AssetClass.INDICES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=1.0, fallback_symbol="^NDX")


class StaticProvider:
    """Provider prezzi deterministico per i test (source dichiarata)."""

    name = "test-feed"

    def __init__(self, price: float):
        self.price = price

    async def quote(self, instrument: Instrument) -> Quote:
        return Quote(epic=instrument.epic, bid=self.price - 0.5, offer=self.price + 0.5, ts=utcnow(), market_status=MarketStatus.TRADEABLE, source=self.name)

    async def candles(self, instrument, **kwargs):
        return []

    async def aclose(self):
        return None


def make_prices(registry: InstrumentRegistry, price: float) -> tuple[PriceService, StaticProvider]:
    provider = StaticProvider(price)
    service = PriceService(registry=registry, ig_provider=provider, public_provider=provider, max_staleness_s=5.0)  # type: ignore[arg-type]
    return service, provider


def make_proposal(quote: Quote, inst: Instrument, direction: Direction = Direction.BUY) -> TradeProposal:
    return TradeProposal(
        trade_id=f"T{utcnow().strftime('%H%M%S%f')}", event_id="E1", strategy_id="D_MACRO_RELEASE", signal_type=SignalType.MACRO_RELEASE, instrument=inst, epic=inst.epic, direction=direction,
        quote=quote, max_entry=quote.price_for(direction) * (1.0004 if direction is Direction.BUY else 0.9996), stop_distance=50.0, limit_distance=100.0, time_horizon_seconds=900,
        expected_return_pct=0.005, expected_loss_pct=0.0025, probability=0.6, confidence=0.7, requested_risk_eur=5.0, reason_code=ReasonCode.MACRO_REPRICING,
        residual=ResidualAlpha(epic=inst.epic, direction=direction, expected_move_pct=0.006, realized_move_pct=0.001, residual_move_pct=0.005, costs=CostEstimate(spread_pct=0.00005), net_alpha_pct=0.004, passes=True),
        explanation=["test"],
    )


async def build_engine(mode: ExecutionMode, price: float = 20000.0):
    settings = load_settings(execution_mode=mode.value, redis_url=None, risk={"bankroll": 1000.0, "max_stake_abs": 50.0, "max_data_staleness_s": 5.0})
    registry = InstrumentRegistry([nasdaq()])
    prices, provider = make_prices(registry, price)
    paper = PaperBroker(starting_balance=1000.0)
    engine = ExecutionEngine(settings=settings, registry=registry, prices=prices, paper=paper, kill_switch=KillSwitch())
    engine.risk_engine._limits = settings.risk
    return engine, provider, registry


async def test_paper_open_close_pnl(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    proposal = make_proposal(quote, inst)
    decision = await eng.assess(proposal)
    assert decision.approved, decision.rejection_reasons
    result = await eng.submit(proposal, decision, quote=quote)
    assert result.status == OrderStatus.FILLED.value
    assert result.deal_id.startswith("PAPER-")
    assert result.fill_price >= quote.offer  # si compra all'offer + slippage
    from core.db import session_scope

    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.status == PositionStatus.OPEN.value
        assert pos.stop_level == pytest.approx(result.fill_price - 50.0)
        order = await Repository(session).get_order(result.client_order_id)
        assert order.status == "FILLED" and order.purpose == "OPEN"

    # prezzo sale di 80 punti -> chiusura manuale in profitto
    provider.price = 20080.0
    eng.prices._live.clear()
    close = await eng.close_position(proposal.trade_id, reason=ExitReason.MANUAL, by="tester")
    assert close.status == OrderStatus.FILLED.value
    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.status == PositionStatus.CLOSED.value
        assert pos.realized_pnl > 0
        assert pos.exit_reason == "MANUAL"
    assert eng.paper.balance > 1000.0


async def test_paper_stop_hit_via_monitor(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    proposal = make_proposal(quote, inst)
    decision = await eng.assess(proposal)
    result = await eng.submit(proposal, decision, quote=quote)
    monitor = PositionMonitor(eng)
    provider.price = 19900.0  # sotto lo stop (fill - 50)
    eng.prices._live.clear()
    summary = await monitor.tick()
    assert summary["closed"] == 1
    from core.db import session_scope

    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.status == PositionStatus.CLOSED.value
        assert pos.exit_reason == "STOP_HIT"
        # perdita ~ rischio dichiarato (stop 50 punti x size)
        assert pos.realized_pnl == pytest.approx(-decision.risk_eur, rel=0.05)
    assert result.deal_id not in eng.paper.positions


async def test_time_stop(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    proposal = make_proposal(quote, inst)
    decision = await eng.assess(proposal)
    await eng.submit(proposal, decision, quote=quote)
    from core.db import session_scope

    async with session_scope() as session:
        await Repository(session).update_position(proposal.trade_id, max_holding_until=utcnow() - timedelta(seconds=1))
    summary = await PositionMonitor(eng).tick()
    assert summary["closed"] == 1
    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.exit_reason == "TIME_STOP"


async def test_thesis_invalidation_pre_event_level(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    proposal = make_proposal(quote, inst)
    decision = await eng.assess(proposal)
    await eng.submit(proposal, decision, quote=quote)
    from core.db import session_scope

    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        await Repository(session).update_position(proposal.trade_id, exit_criteria={**pos.exit_criteria, "pre_event_price": 19990.0})
    provider.price = 19985.0  # torna sotto il livello pre-evento ma sopra lo stop (fill-50 ~ 19950)
    eng.prices._live.clear()
    summary = await PositionMonitor(eng).tick()
    assert summary["closed"] == 1
    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.exit_reason == "THESIS_INVALIDATED"


async def test_shadow_non_invia_ordini(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.SHADOW)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    proposal = make_proposal(quote, inst)
    decision = await eng.assess(proposal)
    result = await eng.submit(proposal, decision, quote=quote)
    assert result.raw.get("shadow") is True
    assert not eng.paper.positions  # nessun fill nel paper broker
    from core.db import session_scope

    async with session_scope() as session:
        pos = await Repository(session).get_position(proposal.trade_id)
        assert pos.mode == "SHADOW" and pos.deal_id.startswith("SHADOW")


async def test_paper_rifiuta_mercato_chiuso(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = Quote(epic=EPIC, bid=19999.5, offer=20000.5, market_status=MarketStatus.CLOSED, source="test-feed")
    request = OrderRequest(client_order_id="c1", trade_id="t1", epic=EPIC, direction=Direction.BUY, size=0.1, max_entry=20010, reference_price=20000.5, stop_distance=50, reason_code=ReasonCode.MACRO_REPRICING)
    with pytest.raises(Exception):
        await eng.paper.open(request, inst, quote)


async def test_paper_slippage_guard(engine, bus, memory_cache):
    eng, provider, registry = await build_engine(ExecutionMode.PAPER)
    inst = registry.get(EPIC)
    quote = await eng.prices.quote(EPIC)
    request = OrderRequest(client_order_id="c2", trade_id="t2", epic=EPIC, direction=Direction.BUY, size=0.1, max_entry=quote.offer - 1, reference_price=quote.offer, stop_distance=50, reason_code=ReasonCode.MACRO_REPRICING)
    result = await eng.paper.open(request, inst, quote)
    assert result.status == OrderStatus.REJECTED.value
    assert "SLIPPAGE_GUARD" in (result.error or "")


# ------------------------------------------------------------- IG mockato
def ig_transport(state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/session") and request.method == "POST":
            return httpx.Response(200, json={"currentAccountId": "Z6DTVR", "lightstreamerEndpoint": "https://demo-apd.marketdatasystems.com", "currencyIsoCode": "EUR", "accountType": "CFD", "accounts": [{"accountId": "Z6DTVR", "accountType": "CFD", "preferred": True}], "accountInfo": {"balance": 30000, "deposit": 0, "profitLoss": 0, "available": 30000}}, headers={"CST": "cst-token", "X-SECURITY-TOKEN": "xst-token"})
        if path.endswith("/accounts"):
            return httpx.Response(200, json={"accounts": [{"accountId": "Z6DTVR", "currency": "EUR", "balance": {"balance": 30000, "deposit": 150, "profitLoss": 12.5, "available": 29862.5}}]})
        if path.endswith("/positions/otc") and request.method == "POST":
            state["last_payload"] = request.read()
            if request.headers.get("_method") == "DELETE":
                return httpx.Response(200, json={"dealReference": "CLOSEREF"})
            return httpx.Response(200, json={"dealReference": "OPENREF"})
        if "/confirms/OPENREF" in path:
            state["confirm_calls"] = state.get("confirm_calls", 0) + 1
            if state["confirm_calls"] == 1:
                return httpx.Response(404, json={"errorCode": "error.confirms.deal-not-found"})
            return httpx.Response(200, json={"dealReference": "OPENREF", "dealId": "DIAAAAB", "dealStatus": "ACCEPTED", "status": "OPEN", "epic": EPIC, "direction": "BUY", "size": 0.1, "level": 20001.0, "stopLevel": 19951.0, "limitLevel": 20101.0, "date": "2026-08-27T01:00:00.000"})
        if "/confirms/CLOSEREF" in path:
            return httpx.Response(200, json={"dealReference": "CLOSEREF", "dealId": "DIAAAAB", "dealStatus": "ACCEPTED", "status": "CLOSED", "epic": EPIC, "direction": "SELL", "size": 0.1, "level": 20040.0, "profit": 3.9, "profitCurrency": "EUR"})
        if path.endswith("/positions"):
            return httpx.Response(200, json={"positions": [{"position": {"dealId": "DIAAAAB", "direction": "BUY", "size": 0.1, "level": 20001.0, "stopLevel": 19951.0, "limitLevel": 20101.0, "currency": "EUR", "createdDateUTC": "2026-08-27T01:00:00"}, "market": {"epic": EPIC, "bid": 20030.0, "offer": 20031.0, "marketStatus": "TRADEABLE", "instrumentName": "US Tech 100"}}]})
        if "/markets/" in path:
            return httpx.Response(200, json={"instrument": {"epic": EPIC, "name": "US Tech 100", "type": "INDICES", "currencies": [{"code": "USD", "isDefault": True}], "lotSize": 1, "contractSize": "1", "marginFactor": 5, "marginFactorUnit": "PERCENTAGE", "valueOfOnePip": "1", "controlledRiskAllowed": True, "streamingPricesAvailable": True, "openingHours": {"marketTimes": [{"openTime": "23:00", "closeTime": "22:00"}]}}, "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 0.1}, "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": 5}, "maxStopOrLimitDistance": {"unit": "PERCENTAGE", "value": 75}}, "snapshot": {"marketStatus": "TRADEABLE", "bid": 20000.0, "offer": 20001.0, "updateTimeUTC": "01:00:00", "scalingFactor": 1, "percentageChange": 0.4}})
        if path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{"epic": EPIC, "instrumentName": "US Tech 100", "instrumentType": "INDICES", "expiry": "-", "bid": 20000, "offer": 20001, "marketStatus": "TRADEABLE"}]})
        return httpx.Response(404, json={"errorCode": "not-mocked " + path})

    return httpx.MockTransport(handler)


async def test_ig_client_login_order_confirm_reconcile():
    from core.enums import IGEnvironment
    from execution.ig.client import IGClient
    from execution.ig.orders import IGOrderGateway
    from execution.ig.positions import parse_account_state, parse_broker_position

    settings = load_settings(redis_url=None, ig={"demo": {"api_key": "k", "username": "u", "password": "p", "account_id": "Z6DTVR"}})
    state: dict = {}
    http = httpx.AsyncClient(transport=ig_transport(state), base_url=settings.ig.demo_base_url)
    client = IGClient(IGEnvironment.DEMO, settings.ig, http=http)
    session = await client.authenticate()
    assert session.account_id == "Z6DTVR" and session.cst == "cst-token"
    account = parse_account_state(await client.get_account(), account_id="Z6DTVR")
    assert account.equity == pytest.approx(30012.5) and account.margin_used == 150

    details = await client.get_market_details(EPIC)
    from market.instrument_registry import apply_ig_details

    inst = apply_ig_details(nasdaq(), details)
    assert inst.min_stop_distance == 5 and inst.market_status is MarketStatus.TRADEABLE and inst.spread == pytest.approx(1.0)

    gateway = IGOrderGateway(client, confirm_attempts=3, confirm_interval_s=0.01)
    request = OrderRequest(client_order_id="ATS123", trade_id="T1", epic=EPIC, direction=Direction.BUY, size=0.1, max_entry=20010, reference_price=20001.0, stop_distance=50, limit_distance=100, reason_code=ReasonCode.MACRO_REPRICING)
    result = await gateway.open(request, currency="USD")
    assert result.status == "FILLED" and result.deal_id == "DIAAAAB" and result.confirmation.accepted
    assert state["confirm_calls"] == 2  # ha atteso la conferma dopo il primo 404 (patch sez. 24)
    import json

    payload = json.loads(state["last_payload"])
    assert payload["stopDistance"] == 50 and payload["limitDistance"] == 100 and payload["direction"] == "BUY" and payload["forceOpen"] is True

    positions = [parse_broker_position(p) for p in await client.get_positions()]
    assert positions[0].deal_id == "DIAAAAB" and positions[0].size == 0.1

    close = await gateway.close(client_order_id="CLS1", trade_id="T1", deal_id="DIAAAAB", epic=EPIC, direction=Direction.BUY, size=0.1, reference_price=20030.0)
    assert close.status == "FILLED" and close.raw["profit"] == 3.9
    payload = json.loads(state["last_payload"])
    assert payload["direction"] == "SELL" and payload["dealId"] == "DIAAAAB"
    await http.aclose()


async def test_ig_ambienti_separati():
    from core.enums import IGEnvironment
    from execution.ig.auth import IGAuthenticator

    settings = load_settings(redis_url=None, ig={"demo": {"api_key": "demo-key", "username": "u", "password": "p"}})
    demo = IGAuthenticator(IGEnvironment.DEMO, settings.ig)
    live = IGAuthenticator(IGEnvironment.LIVE, settings.ig)
    assert demo.configured and not live.configured
    assert demo.base_url.startswith("https://demo-api") and live.base_url.startswith("https://api.ig.com")
    with pytest.raises(Exception):
        await live.authenticate()


def test_live_richiede_credenziali_live():
    with pytest.raises(Exception):
        load_settings(execution_mode="LIVE", redis_url=None, ig={"demo": {"api_key": "k", "username": "u", "password": "p"}})
