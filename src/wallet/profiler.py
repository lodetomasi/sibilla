"""Wallet metrics (sez. 5.2, 7): calcolo deterministico da wallet_trades.

Point-in-time: `until` limita i trade usati, cosi nessun ranking vede il futuro (sez. 6).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from core.schemas import WalletMetrics


def _side_sign(side: str) -> int:
    return 1 if str(side).upper() in ("BUY", "LONG") else -1


def compute_wallet_metrics(
    address: str,
    trades: list[Any],
    *,
    category: str = "ALL",
    since: datetime | None = None,
    until: datetime | None = None,
    resolutions: dict[str, float] | None = None,
) -> WalletMetrics:
    """`trades` = righe WalletTrade (o oggetti con gli stessi attributi).

    `resolutions`: condition_id -> payout dell'outcome (1.0 se vinto, 0.0 se perso) per
    i mercati risolti; usato per il realized P&L per-mercato. Senza risoluzione, il P&L
    si stima da acquisti/vendite (round trip).
    """
    rows = [t for t in trades if (since is None or t.ts >= since) and (until is None or t.ts <= until)]
    if category != "ALL":
        rows = [t for t in rows if t.category == category]
    metrics = WalletMetrics(address=address, category=category, window_start=since, window_end=until)
    if not rows:
        return metrics

    rows.sort(key=lambda t: t.ts)
    metrics.n_trades = len(rows)
    by_market: dict[str, list[Any]] = defaultdict(list)
    for t in rows:
        by_market[str(t.condition_id or t.market_external_id or t.asset_id)].append(t)
    metrics.n_markets = len(by_market)
    metrics.total_volume = sum(float(t.usd_size or 0.0) for t in rows)

    pnls: list[float] = []
    holding_times: list[float] = []
    entries: list[float] = []
    exits: list[float] = []
    equity_curve: list[float] = []
    cumulative = 0.0
    category_volume: dict[str, float] = defaultdict(float)
    market_exposure: dict[str, float] = defaultdict(float)

    for market_id, market_trades in by_market.items():
        buys = [t for t in market_trades if _side_sign(t.side) > 0]
        sells = [t for t in market_trades if _side_sign(t.side) < 0]
        bought = sum(float(t.size) for t in buys)
        sold = sum(float(t.size) for t in sells)
        cost = sum(float(t.size) * float(t.price) for t in buys)
        proceeds = sum(float(t.size) * float(t.price) for t in sells)
        avg_entry = cost / bought if bought else 0.0
        if buys:
            entries.append(avg_entry)
        if sells:
            exits.append(proceeds / sold if sold else 0.0)
        net_size = bought - sold
        pnl = proceeds - cost
        payout = (resolutions or {}).get(market_id)
        if payout is not None and net_size > 0:
            pnl += net_size * payout
        elif net_size > 0 and market_trades[-1].realized_pnl is not None:
            pnl = float(market_trades[-1].realized_pnl)
        elif net_size > 0:
            # posizione ancora aperta senza risoluzione: usa il prezzo dell'ultimo trade noto
            pnl += net_size * float(market_trades[-1].price)
        pnls.append(pnl)
        cumulative += pnl
        equity_curve.append(cumulative)
        if buys and sells:
            holding_times.append((sells[-1].ts - buys[0].ts).total_seconds())
        for t in market_trades:
            category_volume[str(t.category)] += float(t.usd_size or 0.0)
        market_exposure[market_id] += cost

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    metrics.realized_pnl = sum(pnls)
    total_cost = sum(market_exposure.values())
    metrics.roi = metrics.realized_pnl / total_cost if total_cost else 0.0
    metrics.win_rate = len(wins) / len(pnls) if pnls else 0.0
    metrics.avg_entry_price = statistics.fmean(entries) if entries else 0.0
    metrics.avg_exit_price = statistics.fmean(exits) if exits else 0.0
    metrics.avg_winning_trade = statistics.fmean(wins) if wins else 0.0
    metrics.avg_losing_trade = statistics.fmean(losses) if losses else 0.0
    metrics.payoff_ratio = (metrics.avg_winning_trade / abs(metrics.avg_losing_trade)) if losses and metrics.avg_losing_trade else (float("inf") if wins else 0.0)
    if math.isinf(metrics.payoff_ratio):
        metrics.payoff_ratio = 10.0
    gross_loss = abs(sum(losses))
    metrics.profit_factor = (sum(wins) / gross_loss) if gross_loss else (10.0 if wins else 0.0)
    metrics.max_drawdown = _max_drawdown(equity_curve)
    metrics.sharpe_like = _sharpe(pnls)
    metrics.sortino_like = _sortino(pnls)
    metrics.avg_holding_time_s = statistics.fmean(holding_times) if holding_times else 0.0
    if metrics.total_volume:
        metrics.category_distribution = {k: v / metrics.total_volume for k, v in category_volume.items()}
    if total_cost:
        shares = [v / total_cost for v in market_exposure.values()]
        metrics.exposure_concentration = sum(s * s for s in shares)  # HHI
    span_days = max(1.0, (rows[-1].ts - rows[0].ts).total_seconds() / 86400)
    metrics.trade_frequency_per_day = len(rows) / span_days
    sizes = [float(t.usd_size or 0.0) for t in rows]
    metrics.avg_trade_size = statistics.fmean(sizes) if sizes else 0.0
    metrics.median_trade_size = statistics.median(sizes) if sizes else 0.0
    clv = [t.clv.get("clv") for t in rows if isinstance(t.clv, dict) and t.clv.get("clv") is not None]
    drift = [t.clv.get("drift_5m") for t in rows if isinstance(t.clv, dict) and t.clv.get("drift_5m") is not None]
    metrics.clv_edge = statistics.fmean(clv) if clv else 0.0
    metrics.post_entry_drift = statistics.fmean(drift) if drift else 0.0
    metrics.information_advantage = metrics.clv_edge * math.sqrt(len(clv)) if clv else 0.0
    return metrics


def compute_clv(entry_price: float, side: str, price_path: dict[str, float | None], closing_price: float | None, outcome: float | None) -> dict[str, float | None]:
    """Sez. 7: CLV, drift post-entry e impatto a breve.

    price_path: {"+1m": p, "+5m": p, "+30m": p, "+1h": p}.
    """
    sign = _side_sign(side)
    out: dict[str, float | None] = {}
    for label, price in price_path.items():
        out[f"drift_{label.strip('+')}"] = (price - entry_price) * sign if price is not None else None
    out["clv"] = (closing_price - entry_price) * sign if closing_price is not None else None
    out["outcome_edge"] = (outcome - entry_price) * sign if outcome is not None else None
    out["short_term_impact"] = out.get("drift_1m")
    return out


def _max_drawdown(curve: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return max_dd


def _sharpe(pnls: list[float]) -> float:
    if len(pnls) < 3:
        return 0.0
    sd = statistics.pstdev(pnls)
    return (statistics.fmean(pnls) / sd) * math.sqrt(len(pnls)) if sd else 0.0


def _sortino(pnls: list[float]) -> float:
    if len(pnls) < 3:
        return 0.0
    downside = [p for p in pnls if p < 0]
    if not downside:
        return 10.0
    dd = math.sqrt(sum(p * p for p in downside) / len(pnls))
    return (statistics.fmean(pnls) / dd) * math.sqrt(len(pnls)) if dd else 0.0
