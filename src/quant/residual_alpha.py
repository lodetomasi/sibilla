"""Residual Alpha e Cost Model (patch sez. 7, 19, 20, 26, 41).

    Residual Alpha = expected total repricing - observed repricing
    NET ALPHA      = residual - spread - commission - slippage - financing - impact
    Trade consentito solo se NET ALPHA > safety margin.
"""
from __future__ import annotations

from core.enums import Direction
from core.schemas import CostEstimate, Instrument, MarketReaction, Quote, ResidualAlpha

INTRADAY_S = 8 * 3600


def estimate_costs(
    instrument: Instrument,
    quote: Quote,
    *,
    holding_seconds: int,
    expected_slippage_pct: float | None = None,
    size_notional: float | None = None,
) -> CostEstimate:
    """Costi round-trip in frazione del prezzo."""
    spread_pct = quote.spread_pct  # si paga lo spread una volta (entri all'offer, esci al bid)
    commission_pct = instrument.commission_pct * 2  # ingresso + uscita (equity CFD)
    slippage_pct = expected_slippage_pct if expected_slippage_pct is not None else min(spread_pct * 0.5, 0.0005)
    financing_pct = holding_cost_pct(instrument, holding_seconds)
    impact_pct = 0.0
    if size_notional and size_notional > 0:
        # impatto trascurabile per size retail; cresce linearmente oltre 100k nozionali
        impact_pct = max(0.0, (size_notional - 100_000) / 100_000) * spread_pct * 0.1
    return CostEstimate(
        spread_pct=spread_pct,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
        financing_pct=financing_pct,
        market_impact_pct=impact_pct,
    )


def holding_cost_pct(instrument: Instrument, holding_seconds: int) -> float:
    """holding_cost_estimator (patch sez. 20): overnight funding solo oltre l'intraday."""
    if holding_seconds <= INTRADAY_S:
        return 0.0
    nights = max(1, int(holding_seconds // 86400) + (1 if holding_seconds % 86400 else 0))
    return instrument.overnight_funding_pct_annual / 365.0 * nights


def compute_residual_alpha(
    *,
    instrument: Instrument,
    quote: Quote,
    direction: Direction,
    expected_move_pct: float,
    reaction: MarketReaction | None,
    holding_seconds: int,
    safety_margin_pct: float,
    min_net_alpha_pct: float,
    expected_slippage_pct: float | None = None,
) -> ResidualAlpha:
    realized = reaction.realized_move if reaction else 0.0
    # nel verso del trade: positivo se il prezzo si e' gia mosso a nostro favore
    realized_dir = realized * direction.sign
    expected_abs = abs(expected_move_pct)
    residual = expected_abs - realized_dir
    costs = estimate_costs(
        instrument, quote, holding_seconds=holding_seconds, expected_slippage_pct=expected_slippage_pct
    )
    net = residual - costs.total_pct - safety_margin_pct
    return ResidualAlpha(
        epic=instrument.epic,
        direction=direction,
        expected_move_pct=expected_abs,
        realized_move_pct=realized_dir,
        residual_move_pct=residual,
        costs=costs,
        safety_margin_pct=safety_margin_pct,
        net_alpha_pct=net,
        passes=net > 0 and net >= min_net_alpha_pct,
        reaction=reaction,
    )


def slippage_pct(fill_price: float, reference_price: float, direction: Direction) -> float:
    """Slippage subito (positivo = peggiore del riferimento)."""
    if reference_price <= 0:
        return 0.0
    diff = (fill_price - reference_price) / reference_price
    return diff if direction is Direction.BUY else -diff
