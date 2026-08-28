"""Sez. 80 + patch sez. 41: test automatici che dimostrano le iron rules.

LLM cannot exceed max stake / disable risk engine / access secrets / choose leverage /
place malformed orders / trade non-tradeable markets / trade stale data / trade without stop /
trade with negative net alpha / exceed margin.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.clock import utcnow
from core.config import RiskLimits
from core.enums import AssetClass, Direction, ExecutionMode, MarketStatus, ReasonCode, SignalType
from core.errors import RiskViolation
from core.schemas import (
    AccountState,
    CostEstimate,
    Instrument,
    PortfolioOutput,
    Quote,
    ResidualAlpha,
    TradeProposal,
)
from intelligence.contracts import JudgeDecision
from market.instrument_registry import InstrumentRegistry
from risk.correlation import OpenExposure
from risk.engine import PortfolioContext, RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import update_limits_human

LIMITS = RiskLimits(bankroll=1000.0, max_risk_per_trade=0.005, max_open_risk=0.02, max_daily_loss=0.02, max_stake_abs=50.0, min_reward_risk=1.5, max_margin_usage=0.2, min_free_margin=0.7)


def nasdaq() -> Instrument:
    return Instrument(epic="IX.D.NASDAQ.IFE.IP", name="US Tech 100", asset_class=AssetClass.INDICES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=1.0, factors={})


def quote(*, price: float = 20000.0, age_s: float = 0.0, status: MarketStatus = MarketStatus.TRADEABLE, source: str = "ig-stream") -> Quote:
    return Quote(epic="IX.D.NASDAQ.IFE.IP", bid=price - 0.5, offer=price + 0.5, ts=utcnow() - timedelta(seconds=age_s), market_status=status, source=source)


def residual(net: float = 0.004) -> ResidualAlpha:
    return ResidualAlpha(epic="IX.D.NASDAQ.IFE.IP", direction=Direction.BUY, expected_move_pct=0.006, realized_move_pct=0.001, residual_move_pct=0.005, costs=CostEstimate(spread_pct=0.00005), net_alpha_pct=net, passes=net > 0)


def proposal(**overrides) -> TradeProposal:
    q = overrides.pop("quote", quote())
    base = dict(
        trade_id="T1", event_id="E1", strategy_id="D_MACRO_RELEASE", signal_type=SignalType.MACRO_RELEASE, instrument=nasdaq(), epic="IX.D.NASDAQ.IFE.IP",
        direction=Direction.BUY, quote=q, max_entry=q.offer * 1.0004, stop_distance=50.0, limit_distance=100.0, time_horizon_seconds=900,
        expected_return_pct=0.005, expected_loss_pct=0.0025, probability=0.6, confidence=0.7, requested_risk_eur=5.0, reason_code=ReasonCode.MACRO_REPRICING, residual=residual(),
    )
    base.update(overrides)
    return TradeProposal(**base)


def account(equity: float = 1000.0, margin_used: float = 0.0) -> AccountState:
    return AccountState(account_id="PAPER", balance=equity, equity=equity, margin_used=margin_used, deposit=margin_used, available=equity - margin_used)


def context(**overrides) -> PortfolioContext:
    base = dict(account=account(), open_positions=[], realized_pnl_today=0.0, realized_pnl_week=0.0, peak_equity_week=1000.0, trades_today=0, rejected_streak=0)
    base.update(overrides)
    return PortfolioContext(**base)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(limits=LIMITS, registry=InstrumentRegistry([nasdaq()]), kill_switch=KillSwitch(), mode=ExecutionMode.PAPER)


def test_trade_valido_approvato_con_size_dal_rischio(engine):
    decision = engine.evaluate(proposal(), context())
    assert decision.approved, decision.rejection_reasons
    # rischio max = min(1000*0.5%, 50) = 5 EUR; loss/unit = 50 punti * 1 EUR = 50 -> size 0.1
    assert decision.size == pytest.approx(0.1)
    assert decision.risk_eur <= 5.0 + 1e-9
    assert decision.stop_level == pytest.approx(20000.5 - 50)
    assert decision.limit_level == pytest.approx(20000.5 + 100)


def test_llm_non_puo_superare_il_rischio_massimo(engine):
    """Il PM chiede 500 EUR: la size resta quella del cap (0.5% equity)."""
    decision = engine.evaluate(proposal(requested_risk_eur=500.0), context())
    assert decision.approved
    assert decision.risk_eur <= 1000 * LIMITS.max_risk_per_trade + 1e-9
    assert decision.capped_by == "max_risk_per_trade"


def test_llm_non_sceglie_la_leva(engine):
    """La proposal non ha alcun campo size/leverage: la size esce solo dal risk kernel."""
    fields = set(TradeProposal.model_fields) | set(JudgeDecision.model_fields) | set(PortfolioOutput.model_fields)
    assert not any(f in fields for f in ("size", "leverage", "margin", "lot_size", "position_size"))


def test_no_stop_no_trade():
    with pytest.raises(Exception):
        proposal(stop_distance=0.0)
    with pytest.raises(Exception):
        JudgeDecision(decision="ENTER", instrument="US Tech 100", direction=Direction.BUY, stop_distance_pct=0.0, target_distance_pct=0.005, expected_move_pct=0.005, requested_risk_eur=3, confidence=0.7)


def test_mercato_non_tradeable_rifiutato(engine):
    decision = engine.evaluate(proposal(quote=quote(status=MarketStatus.CLOSED)), context())
    assert not decision.approved
    assert any(c.name == "market_tradeable" for c in decision.failed_checks)


def test_feed_stale_rifiutato(engine):
    decision = engine.evaluate(proposal(quote=quote(age_s=60)), context())
    assert not decision.approved
    assert any(c.name == "data_fresh" for c in decision.failed_checks)


def test_prezzo_non_broker_in_live_rifiutato():
    live = RiskEngine(limits=LIMITS, registry=InstrumentRegistry([nasdaq()]), kill_switch=KillSwitch(), mode=ExecutionMode.LIVE)
    decision = live.evaluate(proposal(quote=quote(source="yahoo")), context())
    assert any(c.name == "price_source_is_broker" and not c.passed for c in decision.checks)


def test_residual_alpha_negativo_rifiutato(engine):
    decision = engine.evaluate(proposal(residual=residual(net=-0.001)), context())
    assert not decision.approved
    assert any(c.name == "net_residual_alpha_positive" for c in decision.failed_checks)


def test_reward_risk_sotto_minimo_rifiutato(engine):
    decision = engine.evaluate(proposal(limit_distance=60.0), context())  # R:R 1.2 < 1.5
    assert not decision.approved
    assert any(c.name == "reward_risk" for c in decision.failed_checks)


def test_slippage_guard(engine):
    q = quote()
    decision = engine.evaluate(proposal(quote=q, max_entry=q.offer * 1.01), context())  # 1% > 5bp
    assert any(c.name == "max_entry_within_slippage" and not c.passed for c in decision.checks)


def test_margine_insufficiente_rifiutato(engine):
    decision = engine.evaluate(proposal(), context(account=account(equity=1000.0, margin_used=250.0)))
    assert not decision.approved
    assert any(c.name in ("margin_usage", "min_free_margin") for c in decision.failed_checks)


def test_daily_loss_blocca(engine):
    decision = engine.evaluate(proposal(), context(realized_pnl_today=-25.0))  # 2.5% > 2%
    assert not decision.approved
    assert any(c.name == "daily_loss" for c in decision.failed_checks)


def test_esposizione_correlata_blocca(engine):
    open_pos = [OpenExposure(epic="IX.D.SPTRD.IFE.IP", direction=Direction.BUY, notional=2000.0, risk_eur=14.0, asset_class="INDICES", currency="USD", factors={})]
    registry = InstrumentRegistry([nasdaq(), Instrument(epic="IX.D.SPTRD.IFE.IP", name="US 500", asset_class=AssetClass.INDICES, currency="USD")])
    from core.enums import Factor

    registry.add(nasdaq().model_copy(update={"factors": {Factor.US_EQUITY: 1.0}}))
    registry.add(Instrument(epic="IX.D.SPTRD.IFE.IP", name="US 500", asset_class=AssetClass.INDICES, currency="USD", factors={Factor.US_EQUITY: 1.0}))
    open_pos[0].factors = {Factor.US_EQUITY: 1.0}
    eng = RiskEngine(limits=LIMITS, registry=registry, kill_switch=KillSwitch(), mode=ExecutionMode.PAPER)
    prop = proposal(instrument=registry.get("IX.D.NASDAQ.IFE.IP"))
    decision = eng.evaluate(prop, context(open_positions=open_pos))
    assert not decision.approved
    assert any(c.name in ("max_correlated_exposure", "max_open_risk") for c in decision.failed_checks)


def test_kill_switch_blocca_tutto(engine):
    from core.enums import KillSwitchReason

    engine.kill_switch._active = KillSwitchReason.MANUAL
    decision = engine.evaluate(proposal(), context())
    assert not decision.approved
    assert any(c.name == "kill_switch" for c in decision.failed_checks)


async def test_llm_non_puo_modificare_i_limiti():
    for actor in ("llm", "agent", "portfolio_manager", "LLM:gpt"):
        with pytest.raises(RiskViolation):
            await update_limits_human(actor, {"max_risk_per_trade": 0.2})


def test_limiti_frozen():
    with pytest.raises(Exception):
        LIMITS.max_risk_per_trade = 0.5


def test_ordine_malformato_rifiutato():
    from core.schemas import OrderRequest

    with pytest.raises(Exception):
        OrderRequest(client_order_id="x", trade_id="t", epic="E", direction=Direction.BUY, size=0.0, max_entry=1, reference_price=1, stop_distance=1, reason_code=ReasonCode.MACRO_REPRICING)
    with pytest.raises(Exception):
        OrderRequest(client_order_id="x", trade_id="t", epic="E", direction=Direction.BUY, size=1.0, max_entry=1, reference_price=1, stop_distance=0, reason_code=ReasonCode.MACRO_REPRICING)


def test_secret_non_esposti_negli_output_tool():
    from core.logging import scrub_value

    payload = {"api_key": "sk-or-v1-abc", "nested": {"password": "x", "ok": 1}, "text": "X-IG-API-KEY: 277e1e74a4946c0fb42e20db38781daa7f1cd151"}
    scrubbed = scrub_value(payload)
    assert scrubbed["api_key"] != "sk-or-v1-abc"
    assert scrubbed["nested"]["password"] != "x"
    assert "277e1e74" not in scrubbed["text"]


