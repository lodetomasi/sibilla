from __future__ import annotations

import pytest

from api.etoro_app import parse_calculation_line, parse_feed_line


def test_parse_feed_line_recognizes_known_event() -> None:
    line = "2026-08-28T15:25:21.582585Z [info     ] etoro.runner.no_momentum_candidates scanned=19"
    row = parse_feed_line(line)

    assert row is not None
    assert row["ts"] == "2026-08-28T15:25:21"
    assert row["event"] == "etoro.runner.no_momentum_candidates"
    assert row["label"] == "SCAN: nessun candidato"
    assert row["detail"] == {"scanned": "19"}


def test_parse_feed_line_ignores_unknown_event() -> None:
    line = "2026-08-28T15:17:46.443384Z [info     ] db.engine.created              url=sqlite+aiosqlite:////data/ats.db"
    assert parse_feed_line(line) is None


def test_parse_feed_line_ignores_traceback_lines() -> None:
    assert parse_feed_line("Traceback (most recent call last):") is None
    assert parse_feed_line('  File "/home/ats/x.py", line 1, in main') is None


def test_parse_feed_line_extracts_quoted_values() -> None:
    line = "2026-08-28T15:11:25.687441Z [error    ] etoro.runner.cycle_failed error='HTTP 404: RouteNotFound'"
    row = parse_feed_line(line)

    assert row is not None
    assert row["label"] == "ERRORE CICLO"
    assert row["detail"]["error"] == "HTTP 404: RouteNotFound"


def test_parse_calculation_line_extracts_momentum_evaluation() -> None:
    line = (
        "2026-08-28T15:45:00.123456Z [info     ] etoro.momentum.evaluated       "
        "instrument_id=123 name='Some Co' gap_pct=0.0123 relative_volume=1.45 qualifies=False"
    )
    row = parse_calculation_line(line)

    assert row is not None
    assert row["instrument_id"] == "123"
    assert row["name"] == "Some Co"
    assert row["gap_pct"] == pytest.approx(0.0123)
    assert row["relative_volume"] == pytest.approx(1.45)
    assert row["qualifies"] is False


def test_parse_calculation_line_ignores_other_events() -> None:
    line = "2026-08-28T15:45:00Z [info     ] etoro.runner.started"
    assert parse_calculation_line(line) is None
