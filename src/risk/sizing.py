"""Position sizing dal rischio (patch sez. 12, 26, 27).

    Position Size = Risk Budget / Loss per unit at stop

L'LLM puo chiedere un rischio in EUR (mai leva o size); qui si calcola la size
compatibile con stop distance, valore per punto, passo minimo dello strumento e
cap assoluti. Kelly frazionario e' solo un cap aggiuntivo (sez. 26).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import RiskLimits
from core.enums import Direction
from core.pricing import (
    kelly_fraction,
    limit_level,
    margin_required,
    notional,
    round_to_step,
    size_from_risk,
    stop_level,
)
from core.schemas import Instrument, Quote


@dataclass
class SizingResult:
    size: float
    risk_eur: float
    risk_budget_eur: float
    stop_distance: float
    stop_level: float
    limit_distance: float | None
    limit_level: float | None
    entry_price: float
    notional: float
    margin_required: float
    loss_per_unit: float
    capped_by: str | None
    kelly_cap_eur: float | None
    reward_risk: float | None
    fx_rate: float


def compute_size(
    *,
    instrument: Instrument,
    quote: Quote,
    direction: Direction,
    stop_distance: float,
    limits: RiskLimits,
    equity: float,
    requested_risk_eur: float | None = None,
    risk_fraction_of_max: float = 1.0,
    limit_distance: float | None = None,
    probability: float | None = None,
    fx_rate_to_eur: float = 1.0,
) -> SizingResult:
    """Size dal rischio allo stop; tutti i cap sono deterministici."""
    if stop_distance <= 0:
        raise ValueError("stop_distance deve essere > 0")
    entry = quote.price_for(direction)
    value_per_point_eur = instrument.value_per_point * fx_rate_to_eur

    max_risk_eur = min(equity * limits.max_risk_per_trade, limits.max_stake_abs)
    budget = max_risk_eur * max(0.0, min(1.0, risk_fraction_of_max))
    capped_by: str | None = None
    if requested_risk_eur is not None and requested_risk_eur < budget:
        budget = max(0.0, requested_risk_eur)
        capped_by = "requested_risk"
    elif requested_risk_eur is not None and requested_risk_eur > budget:
        capped_by = "max_risk_per_trade"

    kelly_cap: float | None = None
    if probability is not None and limit_distance:
        rr = limit_distance / stop_distance
        f_star = kelly_fraction(probability, rr)
        kelly_cap = equity * f_star * limits.kelly_fraction
        if kelly_cap < budget:
            budget = kelly_cap
            capped_by = "fractional_kelly"

    loss_per_unit = stop_distance * value_per_point_eur
    raw_size = size_from_risk(budget, stop_distance, value_per_point_eur)
    size = round_to_step(raw_size, instrument.size_step, direction="down")
    if size < instrument.min_size:
        size = 0.0
        capped_by = capped_by or "below_min_size"

    risk_eur = size * loss_per_unit
    return SizingResult(
        size=size,
        risk_eur=risk_eur,
        risk_budget_eur=budget,
        stop_distance=stop_distance,
        stop_level=stop_level(entry, stop_distance, direction.value),
        limit_distance=limit_distance,
        limit_level=limit_level(entry, limit_distance, direction.value) if limit_distance else None,
        entry_price=entry,
        notional=notional(entry, size, value_per_point_eur),
        margin_required=margin_required(entry, size, value_per_point_eur, instrument.margin_factor),
        loss_per_unit=loss_per_unit,
        capped_by=capped_by,
        kelly_cap_eur=kelly_cap,
        reward_risk=(limit_distance / stop_distance) if limit_distance else None,
        fx_rate=fx_rate_to_eur,
    )


def stop_from_volatility(volatility_pct: float | None, price: float, *, multiplier: float = 1.5, floor_pct: float = 0.001) -> float:
    """Stop in punti da volatilita realizzata (patch sez. 15)."""
    pct = max(floor_pct, (volatility_pct or 0.0) * multiplier)
    return price * pct


def stop_from_invalidation(entry: float, invalidation_level: float, direction: Direction, *, buffer_pct: float = 0.0005) -> float:
    """Stop appena oltre il livello che invalida la tesi (es. prezzo pre-evento)."""
    if direction is Direction.BUY:
        return max(1e-9, entry - invalidation_level * (1 - buffer_pct))
    return max(1e-9, invalidation_level * (1 + buffer_pct) - entry)


def stop_from_max_risk(risk_eur: float, size: float, value_per_point: float) -> float:
    if size <= 0 or value_per_point <= 0:
        return 0.0
    return risk_eur / (size * value_per_point)
