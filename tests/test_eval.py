"""Evaluation (performance, calibrazione), backtest."""
from __future__ import annotations

from datetime import timedelta

from core.clock import utcnow
from evaluation.backtest import simulate_event_trades, walk_forward
from evaluation.calibration import calibration_metrics
from evaluation.pnl import performance_metrics


def test_performance_e_calibrazione():
    m = performance_metrics([10, -5, 8, -4, 12], equity_start=1000, days=30)
    assert m["n"] == 5 and m["pnl"] == 21 and m["win_rate"] == 0.6 and m["profit_factor"] > 1 and m["max_drawdown"] == 5
    cal = calibration_metrics([(0.9, 1), (0.8, 1), (0.7, 0), (0.6, 1), (0.3, 0), (0.2, 0), (0.75, 1), (0.65, 0)])
    assert 0 <= cal["brier"] <= 0.25 and cal["ece"] >= 0 and cal["accuracy"] >= 0.5 and cal["curve"]


def test_backtest_no_lookahead_e_stop():
    from core.enums import AssetClass
    from core.schemas import Instrument

    inst = Instrument(epic="X", name="X", asset_class=AssetClass.INDICES, min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=1.0)
    t0 = utcnow() - timedelta(hours=3)
    # prezzo sale 0.8% nei 15 minuti dopo l'evento, poi scende
    series = [(t0 + timedelta(minutes=i), 20000.0) for i in range(60)]
    series += [(t0 + timedelta(minutes=60 + i), 20000.0 * (1 + 0.0008 * min(i, 10))) for i in range(30)]
    events = [{"ts": t0 + timedelta(minutes=60), "epic": "X", "direction": "BUY", "expected_move_pct": 0.01}]
    result = simulate_event_trades(events=events, series_by_epic={"X": series}, instruments={"X": inst}, equity=10000, risk_per_trade=0.005, decision_delay_s=60, reward_risk=1.5, holding_s=900)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.reason == "TARGET_HIT" and t.pnl > 0
    assert t.exit_ts > t.ts + timedelta(seconds=60)  # nessun fill prima della decisione
    # movimento gia avvenuto -> residuo insufficiente -> skip
    late = [{"ts": t0 + timedelta(minutes=60), "epic": "X", "direction": "BUY", "expected_move_pct": 0.001}]
    skipped = simulate_event_trades(events=late, series_by_epic={"X": series}, instruments={"X": inst}, equity=10000, decision_delay_s=600)
    assert not skipped.trades and skipped.skipped.get("no_residual_alpha") == 1
    wf = walk_forward([{**events[0], "ts": t0 - timedelta(days=d)} for d in range(6)], train_days=2, test_days=1, run=lambda tr, te: {"n": len(te)})
    assert wf and all(w["metrics"]["n"] >= 1 for w in wf)
