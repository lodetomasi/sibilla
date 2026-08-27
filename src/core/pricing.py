"""Semantica dei prezzi.

Due mondi convivono:
- Polymarket: prezzo = probabilita in (0,1). Serve solo come intelligence.
- IG CFD: bid/offer in punti dello strumento; il valore monetario di un
  movimento dipende da contract size (valore per punto) e valuta.

Tutte le conversioni vivono qui (patch IG sez. 12, 19, 26).
"""
from __future__ import annotations

from enum import Enum

MIN_PROB = 1e-6
MAX_PROB = 1 - 1e-6


class PriceConvention(str, Enum):
    PROBABILITY = "probability"
    CFD = "cfd"


VENUE_CONVENTION: dict[str, PriceConvention] = {
    "polymarket": PriceConvention.PROBABILITY,
    "ig": PriceConvention.CFD,
    "paper": PriceConvention.CFD,
}


def convention_for(venue: str) -> PriceConvention:
    return VENUE_CONVENTION.get(venue.lower(), PriceConvention.CFD)


# ----------------------------------------------------------------- probabilita
def implied_probability(price: float, convention: PriceConvention) -> float | None:
    if convention is PriceConvention.PROBABILITY:
        return min(max(price, 0.0), 1.0)
    return None


def is_price_acceptable(
    price: float, limit: float, convention: PriceConvention, side: str = "BUY"
) -> bool:
    """`limit` e sempre il prezzo *peggiore* accettabile (sez. 24 / patch sez. 26).

    Per entrambe le convenzioni comprare a un prezzo piu basso e' meglio.
    """
    if side.upper() in ("BUY", "BACK", "LONG"):
        return price <= limit + 1e-12
    return price >= limit - 1e-12


def worse_price(price: float, slippage_pct: float, side: str = "BUY") -> float:
    """Prezzo peggiorato di `slippage_pct` (frazione), usato per max_entry."""
    if side.upper() in ("BUY", "BACK", "LONG"):
        return price * (1 + slippage_pct)
    return price * (1 - slippage_pct)


# --------------------------------------------------------------------- CFD
def entry_price(bid: float, offer: float, direction: str) -> float:
    """Si compra all'offer, si vende al bid."""
    return offer if direction.upper() in ("BUY", "LONG") else bid


def exit_price(bid: float, offer: float, direction: str) -> float:
    """Chiudere un BUY significa vendere al bid, e viceversa."""
    return bid if direction.upper() in ("BUY", "LONG") else offer


def mid(bid: float, offer: float) -> float:
    return (bid + offer) / 2.0


def spread_points(bid: float, offer: float) -> float:
    return max(0.0, offer - bid)


def spread_pct(bid: float, offer: float) -> float:
    m = mid(bid, offer)
    return spread_points(bid, offer) / m if m else 0.0


def points_to_money(points: float, size: float, value_per_point: float) -> float:
    """Movimento in punti -> valuta dello strumento."""
    return points * size * value_per_point


def pnl_points(entry: float, current: float, direction: str) -> float:
    sign = 1 if direction.upper() in ("BUY", "LONG") else -1
    return (current - entry) * sign


def pnl_money(
    entry: float, current: float, direction: str, size: float, value_per_point: float
) -> float:
    return points_to_money(pnl_points(entry, current, direction), size, value_per_point)


def stop_level(entry: float, stop_distance: float, direction: str) -> float:
    return entry - stop_distance if direction.upper() in ("BUY", "LONG") else entry + stop_distance


def limit_level(entry: float, limit_distance: float, direction: str) -> float:
    return entry + limit_distance if direction.upper() in ("BUY", "LONG") else entry - limit_distance


def distance_to_level(entry: float, level: float) -> float:
    return abs(entry - level)


def loss_per_unit_at_stop(stop_distance: float, value_per_point: float) -> float:
    """Perdita per unita di size se lo stop viene colpito (patch sez. 12)."""
    return abs(stop_distance) * value_per_point


def size_from_risk(risk_budget: float, stop_distance: float, value_per_point: float) -> float:
    """Position Size = Risk Budget / Loss per unit at stop (patch sez. 12)."""
    loss_unit = loss_per_unit_at_stop(stop_distance, value_per_point)
    if loss_unit <= 0:
        return 0.0
    return risk_budget / loss_unit


def notional(price: float, size: float, value_per_point: float) -> float:
    """Esposizione nozionale: prezzo x size x valore per punto."""
    return abs(price) * size * value_per_point


def margin_required(price: float, size: float, value_per_point: float, margin_factor_pct: float) -> float:
    """Margine iniziale = nozionale x margin factor (in % IG)."""
    return notional(price, size, value_per_point) * (margin_factor_pct / 100.0)


def effective_leverage(total_notional: float, equity: float) -> float:
    return total_notional / equity if equity > 0 else float("inf")


def reward_risk_ratio(limit_distance: float, stop_distance: float) -> float:
    if stop_distance <= 0:
        return 0.0
    return limit_distance / stop_distance


def round_to_step(value: float, step: float, *, direction: str = "nearest") -> float:
    """Arrotonda size/livelli al passo minimo dello strumento."""
    if step <= 0:
        return value
    ratio = value / step
    if direction == "down":
        units = int(ratio)
    elif direction == "up":
        units = int(-(-ratio // 1))
    else:
        units = round(ratio)
    return round(units * step, 10)


def kelly_fraction(probability: float, reward_risk: float) -> float:
    """f* = (b p - q) / b, b = reward/risk. Usato solo come cap addizionale (sez. 26)."""
    if reward_risk <= 0:
        return 0.0
    p = min(max(probability, 0.0), 1.0)
    q = 1 - p
    return max(0.0, (reward_risk * p - q) / reward_risk)
