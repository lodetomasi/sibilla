"""Adapter TradeProposal per UNA gamba di un pair trade eToro.

Stesso motore di rischio della strategia momentum (leva 5x fissa, USD
nativo, margin_factor 20.0 - vedi risk/etoro_adapter.py per l'iron rule sul
perche' non e' 5.0), ma:
- direzione ESPLICITA (BUY o SELL/short), mai fissa a BUY come nel momentum;
- stop/target simmetrici e piu' stretti (la scommessa e' il ritorno alla
  media dello spread, non un movimento direzionale grande);
- requested_risk_eur va dimezzato dal chiamante (RISK_FRACTION_PER_LEG):
  una coppia ha DUE gambe aperte insieme, il budget di rischio standard va
  diviso tra le due, non raddoppiato.
"""
from __future__ import annotations

import math

from core.enums import AssetClass, Direction, EntryType, ReasonCode, SignalType
from core.schemas import CostEstimate, Instrument, Quote, ResidualAlpha, RiskDecision, TradeProposal

STOP_PCT = 0.04
TARGET_PCT = 0.07  # R:R 1.75 - sopra il min_reward_risk=1.5 del RiskEngine (RiskLimits
# default): un profilo simmetrico 1:1 sarebbe piu' fedele al mean-reversion puro, ma
# il motore di rischio condiviso richiede un minimo di reward/risk, quindi il target
# resta piu' largo dello stop anche qui (aspettativa: lo spread torna oltre la media,
# non solo a meta' strada).
LEVERAGE = 5
MARGIN_FACTOR_PCT = 100.0 / LEVERAGE
RISK_FRACTION_PER_LEG = 0.01  # meta' del RISK_FRACTION_OF_EQUITY standard (0.02): due gambe per trade


def leg_entry_price(direction: Direction, quote: Quote) -> float:
    return quote.offer if direction is Direction.BUY else quote.bid


def leg_stop_and_target(direction: Direction, entry: float) -> tuple[float, float]:
    """Prezzi assoluti (non distanze): per uno short lo stop sta SOPRA l'entry,
    il target SOTTO - l'opposto di una posizione long."""
    if direction is Direction.BUY:
        return round(entry * (1 - STOP_PCT), 2), round(entry * (1 + TARGET_PCT), 2)
    return round(entry * (1 + STOP_PCT), 2), round(entry * (1 - TARGET_PCT), 2)


def build_leg_proposal(
    *,
    instrument_id: int,
    name: str,
    direction: Direction,
    quote: Quote,
    pair_label: str,
    requested_risk_eur: float | None = None,
) -> TradeProposal:
    entry = leg_entry_price(direction, quote)
    stop_distance = entry * STOP_PCT
    limit_distance = entry * TARGET_PCT
    instrument = Instrument(
        epic=quote.epic, name=name, asset_class=AssetClass.EQUITY_CFD, currency="USD",
        min_size=1.0, size_step=1.0, contract_size=1.0, value_per_point=1.0,
        margin_factor=MARGIN_FACTOR_PCT,
    )
    costs = CostEstimate(spread_pct=(quote.offer - quote.bid) / entry if entry > 0 else 0.0, commission_pct=0.0, slippage_pct=0.0005)
    net_alpha = TARGET_PCT - costs.total_pct
    residual = ResidualAlpha(
        epic=quote.epic, direction=direction, expected_move_pct=TARGET_PCT, realized_move_pct=0.0,
        residual_move_pct=TARGET_PCT, costs=costs, net_alpha_pct=net_alpha, passes=net_alpha > 0,
    )
    return TradeProposal(
        trade_id=f"etoro-pair-{instrument_id}-{quote.ts.timestamp():.0f}",
        event_id=pair_label,
        strategy_id="etoro_mean_reversion_pairs",
        signal_type=SignalType.CROSS_ASSET_LAG,
        instrument=instrument,
        epic=quote.epic,
        direction=direction,
        entry_type=EntryType.MARKET,
        quote=quote,
        max_entry=entry,
        stop_distance=stop_distance,
        stop_rationale=f"-{STOP_PCT:.0%} meccanico dall'entry (gamba pair, {pair_label})",
        limit_distance=limit_distance,
        time_horizon_seconds=6 * 3600,
        expected_return_pct=TARGET_PCT,
        expected_loss_pct=STOP_PCT,
        confidence=0.6,
        requested_risk_eur=requested_risk_eur,
        reason_code=ReasonCode.CROSS_ASSET_LAG,
        residual=residual,
    )


def size_from_decision(decision: RiskDecision) -> int:
    return math.floor(decision.size)
