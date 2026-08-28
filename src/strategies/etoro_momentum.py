"""Screener momentum meccanico: gap + volume relativo. Nessun LLM."""
from __future__ import annotations

from dataclasses import dataclass

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle

MIN_GAP_PCT = 0.03
MIN_RELATIVE_VOLUME = 2.0
LOOKBACK_SESSIONS = 20
MIN_HISTORY_LENGTH = LOOKBACK_SESSIONS + 1


@dataclass
class MomentumCandidate:
    instrument_id: int
    name: str
    price: float
    gap_pct: float
    relative_volume: float


@dataclass
class MomentumEvaluation:
    """Il calcolo per OGNI strumento con storico sufficiente, qualificato o no.

    Serve solo a dare visibilita' (log/dashboard) su cosa ha valutato lo
    screener anche quando nessun titolo supera le soglie - senza questo, un
    ciclo a zero candidati e' indistinguibile da uno screener che non ha
    guardato nulla.
    """

    instrument_id: int
    name: str
    price: float
    gap_pct: float
    relative_volume: float
    qualifies: bool


def evaluate_momentum(pairs: list[tuple[InstrumentCandidate, list[DailyCandle]]]) -> list[MomentumEvaluation]:
    out: list[MomentumEvaluation] = []
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
        qualifies = gap_pct >= MIN_GAP_PCT and relative_volume >= MIN_RELATIVE_VOLUME
        out.append(
            MomentumEvaluation(
                instrument_id=instrument.instrument_id, name=instrument.name, price=last.close,
                gap_pct=gap_pct, relative_volume=relative_volume, qualifies=qualifies,
            )
        )
    return out


def momentum_candidates(pairs: list[tuple[InstrumentCandidate, list[DailyCandle]]]) -> list[MomentumCandidate]:
    return [
        MomentumCandidate(
            instrument_id=e.instrument_id, name=e.name, price=e.price,
            gap_pct=e.gap_pct, relative_volume=e.relative_volume,
        )
        for e in evaluate_momentum(pairs)
        if e.qualifies
    ]
