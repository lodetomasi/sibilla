"""Wallet metrics/scoring point-in-time, evaluation (performance, calibrazione, attribution), backtest."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.clock import utcnow
from evaluation.backtest import simulate_event_trades, walk_forward
from evaluation.calibration import calibration_metrics
from evaluation.pnl import performance_metrics
from wallet.persistence import split_windows
from wallet.profiler import compute_clv, compute_wallet_metrics
from wallet.scoring import score_from_metrics, wallet_consensus


def trade(ts, market, side, price, size, category="crypto", wallet="0xa"):
    return SimpleNamespace(ts=ts, condition_id=market, market_external_id=market, asset_id=market, side=side, price=price, size=size, usd_size=price * size, category=category, realized_pnl=None, clv={}, wallet_address=wallet, outcome="Yes")


def test_wallet_metrics_point_in_time():
    t0 = utcnow() - timedelta(days=30)
    trades = [
        trade(t0, "m1", "BUY", 0.40, 100), trade(t0 + timedelta(days=1), "m1", "SELL", 0.60, 100),  # +20
        trade(t0 + timedelta(days=2), "m2", "BUY", 0.50, 100), trade(t0 + timedelta(days=3), "m2", "SELL", 0.45, 100),  # -5
        trade(t0 + timedelta(days=10), "m3", "BUY", 0.30, 100, category="politics"),
    ]
    m = compute_wallet_metrics("0xa", trades, resolutions={"m3": 1.0})
    assert m.n_trades == 5 and m.n_markets == 3
    assert m.realized_pnl == pytest.approx(20 - 5 + 70)
    assert 0 < m.win_rate <= 1 and m.profit_factor > 1
    assert m.category_distribution["crypto"] > m.category_distribution["politics"]
    # point-in-time: fino al giorno 5 il mercato m3 non esiste ancora (no lookahead)
    early = compute_wallet_metrics("0xa", trades, until=t0 + timedelta(days=5))
    assert early.n_markets == 2 and early.realized_pnl == pytest.approx(15)
    crypto_only = compute_wallet_metrics("0xa", trades, category="crypto")
    assert crypto_only.n_trades == 4
    score, components = score_from_metrics(m)
    assert 0 < score < 1 and "clv" in components


def test_clv_e_consensus():
    clv = compute_clv(0.50, "BUY", {"+1m": 0.51, "+5m": 0.53, "+30m": None, "+1h": 0.55}, closing_price=0.60, outcome=1.0)
    assert clv["clv"] == pytest.approx(0.10) and clv["drift_5m"] == pytest.approx(0.03) and clv["drift_30m"] is None
    now = utcnow()
    trades = [trade(now - timedelta(minutes=i), "mX", "BUY", 0.6, 100, wallet=f"0x{i}") for i in range(4)] + [trade(now, "mX", "SELL", 0.6, 50, wallet="0x9")]
    consensus = wallet_consensus(trades, {f"0x{i}": 0.7 for i in range(4)} | {"0x9": 0.6})
    assert consensus["n_wallets"] == 4 and consensus["side"] == "BUY" and consensus["weighted_signal"] > 0.8


def test_split_windows_walk_forward():
    start = utcnow() - timedelta(days=300)
    windows = split_windows(start, utcnow(), train_days=90, test_days=60)
    assert windows and all(a < b < c for a, b, c in windows)
    assert windows[1][0] == windows[0][0] + timedelta(days=60)


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
