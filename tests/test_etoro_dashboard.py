from __future__ import annotations

from api.etoro_app import parse_feed_line


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
