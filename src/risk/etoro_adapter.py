"""Adapter TradeProposal per azioni eToro: USD nativo, leva 5x, stop/take meccanici.

Iron rule valuta: fx_rate_to_eur SEMPRE 1.0 qui. Il RiskEngine e' numericamente
currency-agnostic: i campi *_eur diventano USD per costruzione, mai una
conversione EUR/USD in nessun punto del flusso eToro.

Iron rule margin: margin_factor e' una PERCENTUALE (margin_required =
notional * margin_factor/100 = notional / leverage * 100). Leva 5 (cap ESMA
retail EU/UK su azioni, decisione utente 28/8) = margin_factor 100/5 = 20.0.
MAI confondere margin_factor col moltiplicatore di leva: margin_factor=5.0
implicherebbe leva 20x, non 5x.

Iron rule sizing: la leva NON amplifica il rischio per trade nel sizing
risk-first (compute_size: size = risk_budget / loss_per_unit_at_stop) — il
rischio resta ancorato a requested_risk_eur, deciso dal chiamante (runner),
mai da questo modulo. La leva riduce solo il margine richiesto per la stessa
size, liberando capacita' di margine per piu' posizioni simultanee.
"""
from __future__ import annotations

import math

from core.enums import AssetClass, Direction, EntryType, ReasonCode, SignalType
from core.schemas import CostEstimate, Instrument, Quote, ResidualAlpha, RiskDecision, TradeProposal
from intelligence.etoro_judge import CatalystVerdict
from strategies.etoro_momentum import MomentumCandidate

STOP_PCT = 0.07
TAKE_PROFIT_PCT = 0.14
LEVERAGE = 5  # cap regolamentare ESMA per conti retail EU/UK su azioni (decisione utente 28/8)
MARGIN_FACTOR_PCT = 100.0 / LEVERAGE  # 20.0: percentuale del nozionale richiesta come margine
RISK_FRACTION_OF_EQUITY = 0.02  # rischio per trade di default (decisione utente 28/8, tra 2/5/10/25%)


def build_trade_proposal(
    candidate: MomentumCandidate,
    verdict: CatalystVerdict,
    quote: Quote,
    *,
    event_id: str,
    requested_risk_eur: float | None = None,
) -> TradeProposal:
    entry = quote.offer
    stop_distance = entry * STOP_PCT
    limit_distance = entry * TAKE_PROFIT_PCT
    instrument = Instrument(
        epic=quote.epic,
        name=candidate.name,
        asset_class=AssetClass.EQUITY_CFD,
        currency="USD",
        min_size=1.0,
        size_step=1.0,
        contract_size=1.0,
        value_per_point=1.0,
        margin_factor=MARGIN_FACTOR_PCT,
    )
    costs = CostEstimate(spread_pct=(quote.offer - quote.bid) / entry, commission_pct=0.0, slippage_pct=0.0005)
    net_alpha = TAKE_PROFIT_PCT - costs.total_pct
    residual = ResidualAlpha(
        epic=quote.epic,
        direction=Direction.BUY,
        expected_move_pct=TAKE_PROFIT_PCT,
        realized_move_pct=0.0,
        residual_move_pct=TAKE_PROFIT_PCT,
        costs=costs,
        net_alpha_pct=net_alpha,
        passes=net_alpha > 0,
    )
    return TradeProposal(
        trade_id=f"etoro-{candidate.instrument_id}-{quote.ts.timestamp():.0f}",
        event_id=event_id,
        strategy_id="etoro_momentum_catalyst",
        signal_type=SignalType.COMPANY_EVENT,
        instrument=instrument,
        epic=quote.epic,
        direction=Direction.BUY,
        entry_type=EntryType.MARKET,
        quote=quote,
        max_entry=entry,
        stop_distance=stop_distance,
        stop_rationale=f"-{STOP_PCT:.0%} meccanico dall'entry",
        limit_distance=limit_distance,
        time_horizon_seconds=6 * 3600,
        expected_return_pct=TAKE_PROFIT_PCT,
        expected_loss_pct=STOP_PCT,
        confidence=verdict.confidence,
        requested_risk_eur=requested_risk_eur,
        reason_code=ReasonCode.COMPANY_EVENT,
        residual=residual,
    )


def size_from_decision(decision: RiskDecision) -> int:
    return math.floor(decision.size)
