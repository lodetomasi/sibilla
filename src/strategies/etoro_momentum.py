"""Screener momentum meccanico: gap + volume relativo. Nessun LLM."""
from __future__ import annotations

from dataclasses import dataclass

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle

MIN_GAP_PCT = 0.05
MIN_RELATIVE_VOLUME = 3.0
LOOKBACK_SESSIONS = 20
MIN_HISTORY_LENGTH = LOOKBACK_SESSIONS + 1


@dataclass
class MomentumCandidate:
    instrument_id: int
    name: str
    price: float
    gap_pct: float
    relative_volume: float


def momentum_candidates(pairs: list[tuple[InstrumentCandidate, list[DailyCandle]]]) -> list[MomentumCandidate]:
    out: list[MomentumCandidate] = []
    for instrument, history in pairs:
        if len(history) < MIN_HISTORY_LENGTH:
            continue
        last = history[-1]
        prior = history[-2]
        lookback = history[-1 - LOOKBACK_SESSIONS : -1]
        if prior.close <= 0:
            continue
        gap_pct = (last.close - prior.close) / prior.close
        avg_volume = sum(c.volume for c in lookback) / len(lookback)
        if avg_volume <= 0:
            continue
        relative_volume = last.volume / avg_volume
        if gap_pct >= MIN_GAP_PCT and relative_volume >= MIN_RELATIVE_VOLUME:
            out.append(
                MomentumCandidate(
                    instrument_id=instrument.instrument_id,
                    name=instrument.name,
                    price=last.close,
                    gap_pct=gap_pct,
                    relative_volume=relative_volume,
                )
            )
    return out
