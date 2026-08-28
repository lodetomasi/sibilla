from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.enums import AssetClass, Direction, EntryType, MarketStatus, ReasonCode, SignalType
from core.schemas import AccountState, Quote
from risk.engine import PortfolioContext, RiskEngine
from risk.etoro_pairs_adapter import (
    RISK_FRACTION_PER_LEG,
    STOP_PCT,
    TARGET_PCT,
    build_leg_proposal,
    leg_entry_price,
    leg_stop_and_target,
)


def _quote() -> Quote:
    return Quote(epic="ETORO:1", bid=99.50, offer=100.00, source="etoro-rest", market_status=MarketStatus.TRADEABLE)


def test_leg_entry_price_buy_uses_offer_sell_uses_bid() -> None:
    q = _quote()
    assert leg_entry_price(Direction.BUY, q) == 100.00
    assert leg_entry_price(Direction.SELL, q) == 99.50


def test_leg_stop_and_target_long_stop_below_target_above() -> None:
    stop, target = leg_stop_and_target(Direction.BUY, 100.0)
    assert stop == pytest.approx(100.0 * (1 - STOP_PCT))
    assert target == pytest.approx(100.0 * (1 + TARGET_PCT))
    assert stop < 100.0 < target


def test_leg_stop_and_target_short_stop_above_target_below() -> None:
    # Per uno short il rischio e' che il prezzo SALGA: lo stop deve stare SOPRA
    # l'entry, il target (profitto se scende) SOTTO - l'opposto di un long.
    stop, target = leg_stop_and_target(Direction.SELL, 100.0)
    assert stop == pytest.approx(100.0 * (1 + STOP_PCT))
    assert target == pytest.approx(100.0 * (1 - TARGET_PCT))
    assert target < 100.0 < stop


def test_build_leg_proposal_long_leg() -> None:
    proposal = build_leg_proposal(
        instrument_id=1, name="CorrA", direction=Direction.BUY, quote=_quote(), pair_label="pair-1-2",
    )

    assert proposal.direction == Direction.BUY
    assert proposal.instrument.currency == "USD"
    assert proposal.instrument.margin_factor == pytest.approx(20.0)
    assert proposal.instrument.asset_class == AssetClass.EQUITY_CFD
    assert proposal.entry_type == EntryType.MARKET
    assert proposal.max_entry == 100.00
    assert proposal.reason_code == ReasonCode.CROSS_ASSET_LAG
    assert proposal.signal_type == SignalType.CROSS_ASSET_LAG


def test_build_leg_proposal_short_leg_uses_bid_as_entry() -> None:
    proposal = build_leg_proposal(
        instrument_id=2, name="CorrB", direction=Direction.SELL, quote=_quote(), pair_label="pair-1-2",
    )

    assert proposal.direction == Direction.SELL
    assert proposal.max_entry == 99.50


def test_risk_fraction_per_leg_is_half_the_standard_risk() -> None:
    # Una coppia apre DUE gambe insieme: il budget standard (2%) va diviso, non raddoppiato.
    assert RISK_FRACTION_PER_LEG == pytest.approx(0.01)


def test_risk_engine_approves_both_legs_of_a_pair() -> None:
    account = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    context = PortfolioContext(
        account=account, open_positions=[], realized_pnl_today=0.0, realized_pnl_week=0.0,
        peak_equity_week=100000.0, trades_today=0, rejected_streak=0,
    )
    engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))
    risk_per_leg = account.equity * RISK_FRACTION_PER_LEG

    long_leg = build_leg_proposal(instrument_id=1, name="CorrA", direction=Direction.BUY, quote=_quote(), pair_label="pair-1-2", requested_risk_eur=risk_per_leg)
    short_leg = build_leg_proposal(instrument_id=2, name="CorrB", direction=Direction.SELL, quote=_quote(), pair_label="pair-1-2", requested_risk_eur=risk_per_leg)

    long_decision = engine.evaluate(long_leg, context, fx_rate_to_eur=1.0)
    short_decision = engine.evaluate(short_leg, context, fx_rate_to_eur=1.0)

    assert long_decision.approved is True
    assert short_decision.approved is True
    assert long_decision.size > 0
    assert short_decision.size > 0
