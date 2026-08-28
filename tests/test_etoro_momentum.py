from __future__ import annotations

from datetime import datetime, timezone

import pytest

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle
from strategies.etoro_momentum import MomentumCandidate, momentum_candidates


def _candle(day: int, close: float, volume: float) -> DailyCandle:
    return DailyCandle(
        date=datetime(2026, 8, day, tzinfo=timezone.utc),
        open=close, high=close, low=close, close=close, volume=volume,
    )


def _flat_history(closes_and_volumes: list[tuple[float, float]]) -> list[DailyCandle]:
    return [_candle(i + 1, c, v) for i, (c, v) in enumerate(closes_and_volumes)]


def test_qualifies_on_gap_and_volume_spike() -> None:
    inst = InstrumentCandidate(instrument_id=1, name="PennyCo", price=3.55)
    # 20 sedute piatte a volume 100k, poi ultima seduta +18% a volume 900k (9x)
    history = _flat_history([(3.00, 100_000)] * 20 + [(3.55, 900_000)])

    out = momentum_candidates([(inst, history)])

    assert len(out) == 1
    assert isinstance(out[0], MomentumCandidate)
    assert out[0].instrument_id == 1
    assert out[0].gap_pct == pytest.approx(0.1833, rel=1e-3)
    assert out[0].relative_volume == pytest.approx(9.0, rel=1e-3)


def test_rejects_without_gap() -> None:
    inst = InstrumentCandidate(instrument_id=2, name="Flat", price=3.00)
    history = _flat_history([(3.00, 100_000)] * 20 + [(3.02, 900_000)])  # gap 0.67%

    assert momentum_candidates([(inst, history)]) == []


def test_rejects_without_volume_spike() -> None:
    inst = InstrumentCandidate(instrument_id=3, name="LowVol", price=3.55)
    history = _flat_history([(3.00, 100_000)] * 20 + [(3.55, 150_000)])  # 1.5x, sotto 3x

    assert momentum_candidates([(inst, history)]) == []


def test_requires_minimum_history_length() -> None:
    inst = InstrumentCandidate(instrument_id=4, name="TooNew", price=3.55)
    history = _flat_history([(3.00, 100_000)] * 5 + [(3.55, 900_000)])  # solo 6 candele

    assert momentum_candidates([(inst, history)]) == []
