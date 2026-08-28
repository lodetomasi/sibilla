from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.enums import AssetClass, Direction, EntryType, MarketStatus, ReasonCode, SignalType
from core.schemas import AccountState, Quote, RiskDecision
from intelligence.etoro_judge import CatalystVerdict
from risk.engine import PortfolioContext, RiskEngine
from risk.etoro_adapter import LEVERAGE, RISK_FRACTION_OF_EQUITY, build_trade_proposal, size_from_decision
from strategies.etoro_momentum import MomentumCandidate


def _candidate() -> MomentumCandidate:
    return MomentumCandidate(instrument_id=1, name="PennyCo", price=3.55, gap_pct=0.18, relative_volume=9.0)


def _verdict() -> CatalystVerdict:
    return CatalystVerdict(has_catalyst=True, direction="BUY", confidence=0.7, rationale="FDA approval")


def _quote() -> Quote:
    # market_status=TRADEABLE e' obbligatorio: RiskEngine.evaluate rifiuta sempre
    # (check "market_tradeable") su MarketStatus.UNKNOWN, il default dello schema.
    return Quote(epic="ETORO:1", bid=3.53, offer=3.55, source="etoro-rest", market_status=MarketStatus.TRADEABLE)


def test_leverage_is_5x_esma_retail_cap() -> None:
    assert LEVERAGE == 5


def test_risk_fraction_of_equity_is_2_percent() -> None:
    assert RISK_FRACTION_OF_EQUITY == pytest.approx(0.02)


def test_build_trade_proposal_has_correct_instrument_and_stops() -> None:
    proposal = build_trade_proposal(_candidate(), _verdict(), _quote(), event_id="evt-1")

    assert proposal.epic == "ETORO:1"
    assert proposal.instrument.currency == "USD"
    assert proposal.instrument.margin_factor == pytest.approx(20.0)  # 100/leverage(5)
    assert proposal.instrument.asset_class == AssetClass.EQUITY_CFD
    assert proposal.direction == Direction.BUY
    assert proposal.entry_type == EntryType.MARKET
    assert proposal.max_entry == 3.55
    assert proposal.stop_distance == pytest.approx(3.55 * 0.07, rel=1e-6)
    assert proposal.limit_distance == pytest.approx(3.55 * 0.14, rel=1e-6)
    assert proposal.reason_code == ReasonCode.COMPANY_EVENT
    assert proposal.signal_type == SignalType.COMPANY_EVENT
    assert proposal.residual is not None
    assert proposal.residual.net_alpha_pct > 0


def test_default_requested_risk_is_2_percent_of_equity_when_provided_by_caller() -> None:
    # L'adapter non legge l'equity da solo (non ha un gateway iniettato): il chiamante
    # (runner, Task 10) calcola requested_risk_eur = equity * 0.02 e lo passa qui. Questo
    # test fissa il contratto: se non passato, requested_risk_eur resta None (nessun
    # rischio di default silenzioso, il chiamante DEVE decidere).
    proposal_no_risk = build_trade_proposal(_candidate(), _verdict(), _quote(), event_id="evt-1")
    assert proposal_no_risk.requested_risk_eur is None

    proposal_with_risk = build_trade_proposal(_candidate(), _verdict(), _quote(), event_id="evt-1", requested_risk_eur=2000.0)
    assert proposal_with_risk.requested_risk_eur == 2000.0


def test_risk_engine_approves_with_fx_rate_1_and_usd_account() -> None:
    account = AccountState(account_id="etoro", currency="USD", balance=100000.0, equity=100000.0, available=100000.0, source="etoro-rest")
    context = PortfolioContext(
        account=account, open_positions=[], realized_pnl_today=0.0, realized_pnl_week=0.0,
        peak_equity_week=100000.0, trades_today=0, rejected_streak=0,
    )
    # 2% di 100000 = 2000: rischio per trade di default per questo motore (scelta utente 28/8).
    proposal = build_trade_proposal(_candidate(), _verdict(), _quote(), event_id="evt-1", requested_risk_eur=account.equity * RISK_FRACTION_OF_EQUITY)
    # max_holding_time_s di default (4h) e' calibrato per il vecchio motore Limitless;
    # questo motore tiene una posizione fino al time-stop EOD (~6h30, Task 10/11 alzano
    # il limite via ATS_MAX_HOLDING_TIME_S in produzione) - qui si riflette la stessa config.
    engine = RiskEngine(limits=RiskLimits(max_holding_time_s=8 * 3600))

    decision = engine.evaluate(proposal, context, fx_rate_to_eur=1.0)

    assert decision.approved is True
    assert decision.size > 0
    # Verifica che la leva 5x riduca il margine richiesto rispetto a leva 1 (100%):
    # margin_required = notional * 20/100, quindi 1/5 del nozionale.
    assert decision.margin_required == pytest.approx(decision.notional * 0.20, rel=1e-6)


def test_size_from_decision_rounds_to_whole_units() -> None:
    decision = RiskDecision(approved=True, size=142.7, risk_eur=1000.0, stop_distance=0.25, max_entry=3.55, notional=506.6)

    assert size_from_decision(decision) == 142
