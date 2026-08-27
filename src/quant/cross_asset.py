"""Cross-Asset Confirmation (patch sez. 8) e Cross-Asset Lag (patch sez. 31.C)."""
from __future__ import annotations

from datetime import datetime, timedelta

from core.enums import Direction, Factor
from core.schemas import CrossAssetCheck
from market.instrument_registry import InstrumentRegistry
from quant.features import Series, return_between


def expected_moves_from_factors(
    registry: InstrumentRegistry, factor_shocks: dict[Factor, float]
) -> dict[str, Direction]:
    """Da shock sui fattori (es. RATES -1, RISK_ON +1) alle direzioni attese per strumento."""
    expected: dict[str, Direction] = {}
    for instrument in registry.all():
        score = sum(instrument.factors.get(f, 0.0) * shock for f, shock in factor_shocks.items())
        if abs(score) >= 0.3:
            expected[instrument.epic] = Direction.BUY if score > 0 else Direction.SELL
    return expected


def cross_asset_check(
    *,
    expected: dict[str, Direction],
    series_by_epic: dict[str, Series],
    event_ts: datetime,
    now: datetime,
    min_move_pct: float = 0.0003,
) -> CrossAssetCheck:
    """Confronta direzioni attese e movimenti osservati dal momento dell'evento."""
    observed: dict[str, float] = {}
    confirmations = contradictions = 0
    for epic, direction in expected.items():
        series = series_by_epic.get(epic)
        if not series:
            continue
        r = return_between(series, event_ts - timedelta(seconds=1), now)
        if r is None:
            continue
        observed[epic] = r
        if abs(r) < min_move_pct:
            continue
        if (r > 0) == (direction is Direction.BUY):
            confirmations += 1
        else:
            contradictions += 1
    total = confirmations + contradictions
    score = (confirmations - contradictions) / total if total else 0.0
    if total == 0:
        interpretation = "nessun movimento cross-asset significativo ancora osservabile"
    elif score >= 0.5:
        interpretation = "cross-asset conferma la tesi"
    elif score <= -0.5:
        interpretation = "market interpretation may differ: i mercati correlati si muovono contro la tesi"
    else:
        interpretation = "segnali cross-asset misti"
    return CrossAssetCheck(
        expected=expected,
        observed=observed,
        confirmations=confirmations,
        contradictions=contradictions,
        score=score,
        interpretation=interpretation,
    )


def lag_candidates(
    *,
    registry: InstrumentRegistry,
    series_by_epic: dict[str, Series],
    leader_epic: str,
    event_ts: datetime,
    now: datetime,
    min_leader_move_pct: float = 0.002,
    max_follower_ratio: float = 0.35,
) -> list[dict[str, float | str]]:
    """Strategy C: il leader si e' mosso, i follower correlati non ancora."""
    leader_series = series_by_epic.get(leader_epic)
    if not leader_series:
        return []
    leader_move = return_between(leader_series, event_ts - timedelta(seconds=1), now)
    if leader_move is None or abs(leader_move) < min_leader_move_pct:
        return []
    out: list[dict[str, float | str]] = []
    for follower, corr in registry.related(leader_epic, min_overlap=0.4):
        series = series_by_epic.get(follower.epic)
        if not series:
            continue
        follower_move = return_between(series, event_ts - timedelta(seconds=1), now)
        if follower_move is None:
            continue
        expected_follower = leader_move * corr
        if abs(expected_follower) < min_leader_move_pct * 0.3:
            continue
        ratio = follower_move / expected_follower if expected_follower else 1.0
        if ratio < max_follower_ratio:
            out.append(
                {
                    "epic": follower.epic,
                    "name": follower.name,
                    "correlation_proxy": corr,
                    "leader_move": leader_move,
                    "follower_move": follower_move,
                    "expected_follower_move": expected_follower,
                    "gap": expected_follower - follower_move,
                    "direction": Direction.BUY.value if expected_follower > 0 else Direction.SELL.value,
                }
            )
    out.sort(key=lambda c: -abs(float(c["gap"])))
    return out
