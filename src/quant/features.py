"""Feature store per opportunita (patch sez. 33).

Funzioni pure: serie di prezzi -> feature numeriche. Nessun lookahead: ogni
funzione riceve esplicitamente `now`/`event_ts` e usa solo dati <= now.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

Series = Sequence[tuple[datetime, float]]

HORIZONS_S: dict[str, int] = {"5s": 5, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def _sorted(series: Series) -> list[tuple[datetime, float]]:
    return sorted(((ts, v) for ts, v in series if v is not None), key=lambda p: p[0])


def price_at_or_before(series: Series, ts: datetime) -> float | None:
    """Ultimo prezzo con timestamp <= ts (no lookahead)."""
    best: float | None = None
    for point_ts, value in _sorted(series):
        if point_ts <= ts:
            best = value
        else:
            break
    return best


def price_at_or_after(series: Series, ts: datetime, *, tolerance_s: float = 180) -> float | None:
    for point_ts, value in _sorted(series):
        if point_ts >= ts:
            if (point_ts - ts).total_seconds() <= tolerance_s:
                return value
            return None
    return None


def return_between(series: Series, start: datetime, end: datetime) -> float | None:
    p0 = price_at_or_before(series, start)
    p1 = price_at_or_before(series, end)
    if p0 in (None, 0) or p1 is None:
        return None
    return p1 / p0 - 1.0  # type: ignore[operator]


def returns_after(series: Series, anchor: datetime, entry_price: float, *, now: datetime | None = None) -> dict[str, float | None]:
    """Return dopo 5s/30s/1m/5m/15m/1h dall'anchor vs entry (patch sez. 35)."""
    out: dict[str, float | None] = {}
    for label, seconds in HORIZONS_S.items():
        target = anchor + timedelta(seconds=seconds)
        if now is not None and target > now:
            out[label] = None
            continue
        price = price_at_or_before(series, target)
        out[label] = (price / entry_price - 1.0) if price and entry_price else None
    return out


def log_returns(values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous and current and previous > 0 and current > 0:
            out.append(math.log(current / previous))
    return out


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def realized_volatility(series: Series, *, window_s: float, now: datetime, per_second: bool = False) -> float | None:
    """Volatilita realizzata (dev std dei log-return) nella finestra [now-window, now]."""
    data = [(ts, v) for ts, v in _sorted(series) if now - timedelta(seconds=window_s) <= ts <= now]
    if len(data) < 5:
        return None
    rets = log_returns([v for _, v in data])
    if len(rets) < 3:
        return None
    sigma = stdev(rets)
    if per_second:
        avg_dt = (data[-1][0] - data[0][0]).total_seconds() / max(1, len(data) - 1)
        return sigma / math.sqrt(max(avg_dt, 1e-6))
    return sigma


def atr(candles: Sequence[tuple[float, float, float]], period: int = 14) -> float | None:
    """ATR da tuple (high, low, close)."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for (_, _, prev_close), (high, low, _) in zip(candles, candles[1:], strict=False):
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else None


def zscore(value: float, values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    sigma = stdev(values)
    if sigma == 0:
        return None
    return (value - sum(values) / len(values)) / sigma


def move_sigma(series: Series, *, event_ts: datetime, now: datetime, baseline_window_s: float = 3600) -> float | None:
    """Movimento post-evento espresso in deviazioni standard del periodo pre-evento."""
    pre = [(ts, v) for ts, v in _sorted(series) if event_ts - timedelta(seconds=baseline_window_s) <= ts <= event_ts]
    if len(pre) < 6:
        return None
    rets = log_returns([v for _, v in pre])
    sigma = stdev(rets)
    if sigma == 0:
        return None
    r = return_between(series, event_ts, now)
    if r is None:
        return None
    steps = max(1, len(pre) - 1)
    span_s = (pre[-1][0] - pre[0][0]).total_seconds() or 1.0
    elapsed = max(1.0, (now - event_ts).total_seconds())
    expected_sigma = sigma * math.sqrt(elapsed / (span_s / steps))
    return math.log1p(r) / expected_sigma if expected_sigma else None


def build_feature_vector(
    *,
    event_surprise: float | None,
    source_reliability: float,
    source_freshness_s: float,
    polymarket_probability_change: float | None,
    asset_returns: dict[str, float | None],
    spread_pct: float,
    volatility_pct: float | None,
    liquidity_proxy: float | None,
    cross_asset_confirmation: float | None,
    expected_move: float,
    observed_move: float,
    residual_move: float,
    llm_confidence: float | None = None,
    critic_score: float | None = None,
) -> dict[str, float]:
    """Vettore feature standard (patch sez. 33). Valori None -> omessi."""
    raw: dict[str, float | None] = {
        "event_surprise": event_surprise,
        "source_reliability": source_reliability,
        "source_freshness": source_freshness_s,
        "polymarket_probability_change": polymarket_probability_change,
        "asset_return_5s": asset_returns.get("5s"),
        "asset_return_30s": asset_returns.get("30s"),
        "asset_return_1m": asset_returns.get("1m"),
        "asset_return_5m": asset_returns.get("5m"),
        "spread": spread_pct,
        "volatility": volatility_pct,
        "liquidity_proxy": liquidity_proxy,
        "cross_asset_confirmation": cross_asset_confirmation,
        "expected_move": expected_move,
        "observed_move": observed_move,
        "residual_move": residual_move,
        "LLM_confidence": llm_confidence,
        "critic_score": critic_score,
    }
    return {k: float(v) for k, v in raw.items() if v is not None and not (isinstance(v, float) and math.isnan(v))}
