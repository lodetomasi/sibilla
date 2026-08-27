"""Market microstructure features (sez. 8/9).

Tutte le funzioni sono pure: prendono book/serie e restituiscono numeri. Cosi
sono usabili identiche in live e in backtest (sez. 55, no lookahead).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

from core.schemas import OrderBook


@dataclass
class BookFeatures:
    spread: float | None = None
    spread_pct: float | None = None
    mid: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    order_book_imbalance: float | None = None
    depth_1: float = 0.0
    depth_3: float = 0.0
    depth_5: float = 0.0
    bid_depth_3: float = 0.0
    ask_depth_3: float = 0.0
    weighted_mid: float | None = None
    market_impact_10: float | None = None
    levels_bid: int = 0
    levels_ask: int = 0

    def as_dict(self) -> dict[str, float]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def book_features(book: OrderBook) -> BookFeatures:
    best_bid, best_ask = book.best_bid, book.best_ask
    spread = book.spread
    mid = book.mid
    features = BookFeatures(
        spread=spread,
        mid=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        levels_bid=len(book.bids),
        levels_ask=len(book.asks),
    )
    if spread is not None and mid:
        features.spread_pct = spread / mid if mid else None
    bid_1 = book.bids[0].size if book.bids else 0.0
    ask_1 = book.asks[0].size if book.asks else 0.0
    features.depth_1 = bid_1 + ask_1
    features.bid_depth_3 = sum(level.size for level in book.bids[:3])
    features.ask_depth_3 = sum(level.size for level in book.asks[:3])
    features.depth_3 = features.bid_depth_3 + features.ask_depth_3
    features.depth_5 = sum(level.size for level in book.bids[:5]) + sum(
        level.size for level in book.asks[:5]
    )
    total_1 = bid_1 + ask_1
    if total_1 > 0:
        features.order_book_imbalance = (bid_1 - ask_1) / total_1
    if best_bid is not None and best_ask is not None and total_1 > 0:
        features.weighted_mid = (best_bid * ask_1 + best_ask * bid_1) / total_1
    features.market_impact_10 = market_impact(book, notional=10.0, side="BACK")
    return features


def market_impact(book: OrderBook, *, notional: float, side: str = "BACK") -> float | None:
    """Prezzo medio pagato consumando il book per `notional` unita, meno il best.

    Restituisce lo slippage atteso in punti prezzo. None se il book non basta.
    """
    levels = book.asks if side.upper() in ("BACK", "BUY") else book.bids
    if not levels or notional <= 0:
        return None
    remaining = notional
    cost = 0.0
    for level in levels:
        take = min(remaining, level.size)
        cost += take * level.price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None
    avg_price = cost / notional
    reference = levels[0].price
    return avg_price - reference if side.upper() in ("BACK", "BUY") else reference - avg_price


def fillable_size(book: OrderBook, *, price_limit: float, side: str = "BACK") -> float:
    return book.liquidity_at_or_better(price_limit, side)


@dataclass
class PriceFeatures:
    price: float | None = None
    price_change_1m: float | None = None
    price_change_5m: float | None = None
    price_change_30m: float | None = None
    price_velocity: float | None = None
    price_acceleration: float | None = None
    volatility: float | None = None
    realized_vol_30m: float | None = None
    max_move_zscore: float | None = None
    volume_acceleration: float | None = None
    liquidity_change: float | None = None
    trade_intensity: float | None = None
    samples: int = 0
    extra: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, float]:
        data = {k: v for k, v in asdict(self).items() if k != "extra" and v is not None}
        data.update(self.extra)
        return data


def _series(points: Sequence[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    return sorted(((ts, value) for ts, value in points if value is not None), key=lambda p: p[0])


def price_change_over(
    points: Sequence[tuple[datetime, float]], *, seconds: float, now: datetime | None = None
) -> float | None:
    """Variazione di prezzo nell'ultima finestra (relativa)."""
    data = _series(points)
    if len(data) < 2:
        return None
    reference_ts = (now or data[-1][0])
    target = reference_ts.timestamp() - seconds
    past = None
    for ts, value in data:
        if ts.timestamp() <= target:
            past = value
        else:
            break
    if past is None:
        past = data[0][1]
    latest = data[-1][1]
    if past == 0:
        return None
    return (latest - past) / abs(past)


def price_features(
    prices: Sequence[tuple[datetime, float]],
    *,
    volumes: Sequence[tuple[datetime, float]] | None = None,
    liquidity: Sequence[tuple[datetime, float]] | None = None,
    trades: Sequence[datetime] | None = None,
    now: datetime | None = None,
) -> PriceFeatures:
    data = _series(prices)
    features = PriceFeatures(samples=len(data))
    if not data:
        return features
    features.price = data[-1][1]
    features.price_change_1m = price_change_over(data, seconds=60, now=now)
    features.price_change_5m = price_change_over(data, seconds=300, now=now)
    features.price_change_30m = price_change_over(data, seconds=1800, now=now)

    if len(data) >= 2:
        (t0, p0), (t1, p1) = data[-2], data[-1]
        dt = max(1e-6, (t1 - t0).total_seconds())
        features.price_velocity = (p1 - p0) / dt
    if len(data) >= 3:
        (t0, p0), (t1, p1), (t2, p2) = data[-3], data[-2], data[-1]
        dt1 = max(1e-6, (t1 - t0).total_seconds())
        dt2 = max(1e-6, (t2 - t1).total_seconds())
        v1 = (p1 - p0) / dt1
        v2 = (p2 - p1) / dt2
        features.price_acceleration = (v2 - v1) / max(1e-6, (dt1 + dt2) / 2)

    returns = _returns([value for _, value in data])
    if len(returns) >= 3:
        features.volatility = _stdev(returns)
        recent = returns[-30:]
        features.realized_vol_30m = _stdev(recent) if len(recent) >= 3 else None
        vol = features.volatility or 0.0
        if vol > 0:
            features.max_move_zscore = abs(returns[-1]) / vol

    if volumes:
        vol_data = _series(volumes)
        if len(vol_data) >= 3:
            deltas = [
                vol_data[i][1] - vol_data[i - 1][1] for i in range(1, len(vol_data))
            ]
            baseline = _mean(deltas[:-1]) if len(deltas) > 1 else 0.0
            latest = deltas[-1]
            features.volume_acceleration = (
                (latest - baseline) / abs(baseline) if baseline else (1.0 if latest > 0 else 0.0)
            )
    if liquidity:
        liq_data = _series(liquidity)
        if len(liq_data) >= 2 and liq_data[0][1]:
            features.liquidity_change = (liq_data[-1][1] - liq_data[-2][1]) / abs(liq_data[-2][1] or 1)
    if trades:
        window_start = (now or data[-1][0]).timestamp() - 300
        recent_trades = [t for t in trades if t.timestamp() >= window_start]
        features.trade_intensity = len(recent_trades) / 5.0  # trade/minuto
    return features


def _returns(values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous:
            out.append((current - previous) / abs(previous))
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def zscore(value: float, values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    sigma = _stdev(values)
    if sigma == 0:
        return None
    return (value - _mean(values)) / sigma


def combine_features(*groups: dict[str, float] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for group in groups:
        if group:
            out.update({k: float(v) for k, v in group.items() if isinstance(v, (int, float))})
    return out
