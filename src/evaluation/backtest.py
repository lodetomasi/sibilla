"""Simulation engine (sez. 54-56): replay storico con clock congelato, spread, slippage,
costi, stop/target/time-stop; walk-forward (train/validate/test/roll).

Non usa LLM in replay: valuta la componente deterministica (event -> asset mapping
regolare + market reaction + residual alpha + risk kernel) su eventi storici con
prezzi reali; l'ablation LLM si misura sul journal live/paper (sez. 59).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from core.enums import Direction
from core.pricing import pnl_money
from core.schemas import Instrument
from evaluation.pnl import performance_metrics
from quant.features import Series, price_at_or_before, realized_volatility


@dataclass
class BacktestTrade:
    ts: datetime
    epic: str
    direction: Direction
    entry: float
    exit: float
    exit_ts: datetime
    reason: str
    size: float
    risk_eur: float
    pnl: float
    expected_move_pct: float
    realized_at_entry_pct: float
    residual_pct: float


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    equity_start: float = 0.0

    def metrics(self) -> dict[str, Any]:
        pnls = [t.pnl for t in self.trades]
        m = performance_metrics(pnls, equity_start=self.equity_start)
        r = [t.pnl / t.risk_eur for t in self.trades if t.risk_eur]
        m["mean_r"] = round(sum(r) / len(r), 3) if r else None
        m["skipped"] = self.skipped
        return m


def simulate_event_trades(
    *,
    events: list[dict[str, Any]],
    series_by_epic: dict[str, Series],
    instruments: dict[str, Instrument],
    equity: float,
    risk_per_trade: float = 0.005,
    decision_delay_s: int = 60,
    stop_vol_multiple: float = 1.5,
    reward_risk: float = 1.5,
    holding_s: int = 900,
    slippage_pct: float = 0.0002,
    min_net_alpha_pct: float = 0.0005,
) -> BacktestResult:
    """events: [{ts, epic, direction: 'BUY'|'SELL', expected_move_pct}] ordinati.

    Regole di replay: decisione a ts+delay (latenza), entry all'offer/bid con slippage,
    stop = vol*multiplo, target = R:R*stop, uscita a stop/target/time-stop usando SOLO
    prezzi successivi all'ingresso (no lookahead).
    """
    result = BacktestResult(equity_start=equity)
    for event in events:
        epic = event["epic"]
        series = series_by_epic.get(epic)
        instrument = instruments.get(epic)
        if not series or instrument is None:
            result.skipped["no_data"] = result.skipped.get("no_data", 0) + 1
            continue
        event_ts: datetime = event["ts"]
        decision_ts = event_ts + timedelta(seconds=decision_delay_s)
        direction = Direction.parse(event["direction"])
        before = price_at_or_before(series, event_ts - timedelta(seconds=1))
        at_decision = price_at_or_before(series, decision_ts)
        if not before or not at_decision:
            result.skipped["no_price"] = result.skipped.get("no_price", 0) + 1
            continue
        realized = (at_decision / before - 1) * direction.sign
        expected = abs(float(event["expected_move_pct"]))
        spread_pct = (instrument.spread or 0.0) / at_decision
        residual = expected - realized
        net = residual - spread_pct - slippage_pct
        if net < min_net_alpha_pct:
            result.skipped["no_residual_alpha"] = result.skipped.get("no_residual_alpha", 0) + 1
            continue
        vol = realized_volatility(series, window_s=3600, now=decision_ts) or 0.001
        stop_pct = max(0.0008, vol * stop_vol_multiple)
        half_spread = (instrument.spread or 0.0) / 2
        entry = (at_decision + half_spread) * (1 + slippage_pct) if direction is Direction.BUY else (at_decision - half_spread) * (1 - slippage_pct)
        stop_points = entry * stop_pct
        target_points = stop_points * reward_risk
        risk_eur = equity * risk_per_trade
        size = risk_eur / (stop_points * instrument.value_per_point)
        size = max(instrument.min_size, round(size / instrument.size_step) * instrument.size_step)
        risk_eur = size * stop_points * instrument.value_per_point
        stop_level = entry - stop_points if direction is Direction.BUY else entry + stop_points
        target_level = entry + target_points if direction is Direction.BUY else entry - target_points
        deadline = decision_ts + timedelta(seconds=holding_s)
        exit_px, exit_ts, reason = None, None, "TIME_STOP"
        for ts, mid in series:
            if ts <= decision_ts:
                continue
            if ts > deadline:
                break
            px = mid - half_spread if direction is Direction.BUY else mid + half_spread
            hit_stop = px <= stop_level if direction is Direction.BUY else px >= stop_level
            hit_target = px >= target_level if direction is Direction.BUY else px <= target_level
            if hit_stop:
                exit_px, exit_ts, reason = stop_level, ts, "STOP_HIT"
                break
            if hit_target:
                exit_px, exit_ts, reason = target_level, ts, "TARGET_HIT"
                break
        if exit_px is None:
            last = price_at_or_before(series, deadline)
            if last is None:
                result.skipped["no_exit_price"] = result.skipped.get("no_exit_price", 0) + 1
                continue
            exit_px = last - half_spread if direction is Direction.BUY else last + half_spread
            exit_ts = deadline
        pnl = pnl_money(entry, exit_px, direction.value, size, instrument.value_per_point)
        result.trades.append(BacktestTrade(ts=event_ts, epic=epic, direction=direction, entry=entry, exit=exit_px, exit_ts=exit_ts or deadline, reason=reason, size=size, risk_eur=risk_eur, pnl=pnl, expected_move_pct=expected, realized_at_entry_pct=realized, residual_pct=residual))
        equity += pnl
    return result


def walk_forward(events: list[dict[str, Any]], *, train_days: int, test_days: int, run: Any) -> list[dict[str, Any]]:
    """Sez. 56: train -> validate -> test -> roll. `run(train_events, test_events) -> metrics`."""
    if not events:
        return []
    events = sorted(events, key=lambda e: e["ts"])
    start, end = events[0]["ts"], events[-1]["ts"]
    out: list[dict[str, Any]] = []
    cursor = start
    while cursor + timedelta(days=train_days + test_days) <= end + timedelta(days=1):
        split = cursor + timedelta(days=train_days)
        test_end = split + timedelta(days=test_days)
        train = [e for e in events if cursor <= e["ts"] < split]
        test = [e for e in events if split <= e["ts"] < test_end]
        if train and test:
            out.append({"train": [cursor.isoformat(), split.isoformat()], "test": [split.isoformat(), test_end.isoformat()], "metrics": run(train, test)})
        cursor = cursor + timedelta(days=test_days)
    return out
