from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle
from core.enums import Direction
from strategies.etoro_pairs import LOOKBACK_SESSIONS, find_pair_signals


def _history(closes: list[float]) -> list[DailyCandle]:
    return [
        DailyCandle(date=datetime(2026, 1, 1, tzinfo=timezone.utc), open=c, high=c, low=c, close=c, volume=100_000)
        for c in closes
    ]


def _correlated_series(n: int, *, diverge_last: bool = False, seed: int = 42) -> tuple[list[float], list[float]]:
    # Due serie che seguono lo stesso trend condiviso con rumore indipendente
    # piccolo per ciascuna (correlazione alta ma non 1.0 esatto, rapporto con
    # varianza storica reale - senza rumore il rapporto storico avrebbe
    # varianza zero e lo z-score sarebbe indefinito, mai il caso sui dati reali).
    rng = random.Random(seed)
    a, b = [], []
    price_a, price_b = 100.0, 50.0
    for i in range(n):
        shared_step = 1.0 if i % 2 == 0 else -1.0
        price_a += shared_step + rng.uniform(-0.15, 0.15)
        price_b += shared_step * 0.5 + rng.uniform(-0.08, 0.08)
        a.append(price_a)
        b.append(price_b)
    if diverge_last:
        a[-1] = a[-1] * 1.15  # A schizza in alto rispetto al rapporto storico con B
    return a, b


def test_finds_signal_on_correlated_pair_with_recent_divergence() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="CorrA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="CorrB", price=50.0)
    closes_a, closes_b = _correlated_series(LOOKBACK_SESSIONS + 1, diverge_last=True)
    pairs = [(inst_a, _history(closes_a)), (inst_b, _history(closes_b))]

    signals = find_pair_signals(pairs)

    assert len(signals) == 1
    sig = signals[0]
    assert {sig.instrument_a_id, sig.instrument_b_id} == {1, 2}
    assert sig.correlation >= 0.75
    assert abs(sig.z_score) >= 2.0
    # A e' salito rispetto al rapporto storico -> A va shortato, B comprato
    if sig.instrument_a_id == 1:
        assert sig.direction_a == Direction.SELL
        assert sig.direction_b == Direction.BUY
    else:
        assert sig.direction_a == Direction.BUY
        assert sig.direction_b == Direction.SELL


def test_no_signal_when_pair_uncorrelated() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="RandA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="RandB", price=50.0)
    closes_a = [100.0 + (i % 2) * 3 - 1.5 for i in range(LOOKBACK_SESSIONS + 1)]
    closes_b = [50.0 + ((i + 1) % 3) * 2 - 2 for i in range(LOOKBACK_SESSIONS + 1)]
    pairs = [(inst_a, _history(closes_a)), (inst_b, _history(closes_b))]

    signals = find_pair_signals(pairs)

    assert signals == []


def test_no_signal_when_correlated_but_not_diverged() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="CorrA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="CorrB", price=50.0)
    closes_a, closes_b = _correlated_series(LOOKBACK_SESSIONS + 1, diverge_last=False)
    pairs = [(inst_a, _history(closes_a)), (inst_b, _history(closes_b))]

    signals = find_pair_signals(pairs)

    assert signals == []


def test_requires_minimum_history_length() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="TooNewA", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="TooNewB", price=50.0)
    closes_a, closes_b = _correlated_series(10, diverge_last=True)
    pairs = [(inst_a, _history(closes_a)), (inst_b, _history(closes_b))]

    assert find_pair_signals(pairs) == []


def test_signals_sorted_by_absolute_z_score_descending() -> None:
    inst_a = InstrumentCandidate(instrument_id=1, name="A", price=100.0)
    inst_b = InstrumentCandidate(instrument_id=2, name="B", price=50.0)
    inst_c = InstrumentCandidate(instrument_id=3, name="C", price=100.0)
    inst_d = InstrumentCandidate(instrument_id=4, name="D", price=50.0)
    closes_a, closes_b = _correlated_series(LOOKBACK_SESSIONS + 1, diverge_last=True)
    closes_c, closes_d = _correlated_series(LOOKBACK_SESSIONS + 1, diverge_last=True)
    closes_c[-1] = closes_c[-1] * 1.05  # divergenza minore di A/B -> z piu' basso
    pairs = [
        (inst_a, _history(closes_a)), (inst_b, _history(closes_b)),
        (inst_c, _history(closes_c)), (inst_d, _history(closes_d)),
    ]

    signals = find_pair_signals(pairs)

    assert len(signals) >= 1
    for i in range(len(signals) - 1):
        assert abs(signals[i].z_score) >= abs(signals[i + 1].z_score)
