"""P&L e metriche performance (sez. 57)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.repository import Repository


def performance_metrics(pnls: list[float], *, equity_start: float, days: float | None = None) -> dict[str, float | None]:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "roi": 0.0}
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = abs(sum(losses))
    curve, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        curve += p
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)
    mean = total / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
    downside = math.sqrt(sum(p * p for p in losses) / n) if losses else 0.0
    roi = total / equity_start if equity_start else 0.0
    years = (days / 365.0) if days else None
    cagr = ((1 + roi) ** (1 / years) - 1) if years and years > 0 and roi > -1 else None
    return {
        "n": n, "pnl": round(total, 2), "roi": round(roi, 5), "cagr": round(cagr, 5) if cagr is not None else None,
        "sharpe": round(mean / sd * math.sqrt(n), 3) if sd else None, "sortino": round(mean / downside * math.sqrt(n), 3) if downside else None,
        "max_drawdown": round(max_dd, 2), "max_drawdown_pct": round(max_dd / equity_start, 5) if equity_start else None,
        "calmar": round((total / max_dd), 3) if max_dd else None, "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
        "win_rate": round(len(wins) / n, 3), "payoff_ratio": round((sum(wins) / len(wins)) / (gross_loss / len(losses)), 3) if wins and losses else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0, "expectancy": round(mean, 3),
    }


async def realized_performance(*, mode: str, since: datetime | None = None, equity_start: float | None = None) -> dict[str, Any]:
    async with session_scope() as session:
        repo = Repository(session)
        positions = list(await repo.closed_positions(since=since, mode=mode, limit=10000))
        first_snapshot = (await repo.portfolio_history(mode=mode))[:1]
    start_equity = equity_start or (float(first_snapshot[0].equity) if first_snapshot else 0.0) or 1.0
    pnls = [float(p.realized_pnl or 0.0) for p in positions]
    days = ((positions[-1].closed_at - positions[0].closed_at).total_seconds() / 86400) if len(positions) >= 2 and positions[0].closed_at and positions[-1].closed_at else None
    metrics = performance_metrics(pnls, equity_start=start_equity, days=days)
    by_strategy: dict[str, list[float]] = {}
    by_reason: dict[str, int] = {}
    for p in positions:
        by_strategy.setdefault(p.strategy_id or "unknown", []).append(float(p.realized_pnl or 0.0))
        by_reason[p.exit_reason or "unknown"] = by_reason.get(p.exit_reason or "unknown", 0) + 1
    metrics["by_strategy"] = {k: performance_metrics(v, equity_start=start_equity) for k, v in by_strategy.items()}
    metrics["exit_reasons"] = by_reason
    metrics["mode"] = mode
    metrics["equity_start"] = start_equity
    return metrics


async def execution_quality(*, mode: str, since: datetime | None = None) -> dict[str, Any]:
    """Sez. 57 Execution: slippage, fill rate, latenza."""
    since = since or utcnow() - timedelta(days=30)
    async with session_scope() as session:
        orders = [o for o in await Repository(session).recent_orders(limit=5000) if o.mode == mode and o.created_at >= since]
    opens = [o for o in orders if o.purpose == "OPEN"]
    filled = [o for o in opens if o.status == "FILLED"]
    slips = [float(o.slippage_pct) for o in filled if o.slippage_pct is not None]
    latencies = []
    for o in filled:
        t = o.timings or {}
        if t.get("order_submission_ts") and t.get("confirm_ts"):
            latencies.append((t["confirm_ts"] - t["order_submission_ts"]) * 1000)
        elif t.get("submit_latency_ms"):
            latencies.append(t["submit_latency_ms"])
    return {
        "orders": len(opens), "filled": len(filled), "fill_rate": round(len(filled) / len(opens), 3) if opens else None,
        "rejected": sum(1 for o in opens if o.status == "REJECTED"), "avg_slippage_pct": round(sum(slips) / len(slips), 6) if slips else None,
        "max_slippage_pct": round(max(slips), 6) if slips else None, "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "p95_latency_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1) if latencies else None,
    }
