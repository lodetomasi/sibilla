"""Event study / Market Reaction Analyzer (patch sez. 7).

Quanto del segnale e' gia nel prezzo? Costruisce `MarketReaction` da una serie
di prezzi e da un timestamp evento, usando solo dati <= now.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.enums import Direction
from core.schemas import MarketReaction
from quant.features import Series, price_at_or_after, price_at_or_before, realized_volatility


def build_market_reaction(
    *,
    epic: str,
    series: Series,
    event_ts: datetime,
    now: datetime,
    expected_move_pct: float,
    direction: Direction,
    current_price: float | None = None,
    data_source: str = "",
) -> MarketReaction:
    """expected_move_pct e' l'ampiezza attesa (>=0) nel verso di `direction`."""
    before = price_at_or_before(series, event_ts - timedelta(seconds=1))
    at_event = price_at_or_after(series, event_ts) or before
    current = current_price if current_price is not None else price_at_or_before(series, now)

    def snap(seconds: int) -> float | None:
        target = event_ts + timedelta(seconds=seconds)
        if target > now:
            return None
        return price_at_or_before(series, target)

    realized = 0.0
    if before and current:
        realized = current / before - 1.0
    signed_expected = abs(expected_move_pct) * direction.sign
    # residuo nel verso del trade: quanto manca ancora al target atteso
    residual = (signed_expected - realized) * direction.sign
    vol = realized_volatility(series, window_s=3600, now=now)
    return MarketReaction(
        epic=epic,
        event_ts=event_ts,
        price_before_event=before,
        price_at_event=at_event,
        price_5s_after=snap(5),
        price_30s_after=snap(30),
        price_1m_after=snap(60),
        price_5m_after=snap(300),
        current_price=current,
        realized_move=realized,
        expected_move=signed_expected,
        residual_move=residual,
        volatility_pct=vol,
        data_source=data_source,
    )


def historical_analogue_move(
    events: list[tuple[datetime, float]],
    series: Series,
    *,
    horizon_s: int = 900,
    surprise_sign: int = 1,
) -> dict[str, float | None]:
    """Reazione mediana storica a eventi simili (es. sorprese CPI dello stesso segno).

    `events`: lista (ts, surprise). Ritorna mediana/quantili del return a `horizon_s`.
    """
    moves: list[float] = []
    for ts, surprise in events:
        if surprise_sign and (surprise > 0) != (surprise_sign > 0):
            continue
        p0 = price_at_or_before(series, ts)
        p1 = price_at_or_before(series, ts + timedelta(seconds=horizon_s))
        if p0 and p1:
            moves.append(p1 / p0 - 1.0)
    if not moves:
        return {"n": 0, "median": None, "p25": None, "p75": None, "hit_rate": None}
    moves.sort()
    n = len(moves)
    median = moves[n // 2] if n % 2 else (moves[n // 2 - 1] + moves[n // 2]) / 2
    return {
        "n": n,
        "median": median,
        "p25": moves[int(0.25 * (n - 1))],
        "p75": moves[int(0.75 * (n - 1))],
        "hit_rate": sum(1 for m in moves if (m > 0) == (median > 0)) / n,
    }
