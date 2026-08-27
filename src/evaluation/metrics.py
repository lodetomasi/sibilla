"""Model performance by domain (sez. 38) e failure detection (sez. 61)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.repository import Repository
from evaluation.calibration import calibration_metrics
from intelligence.reliability import reliability_table


async def performance_by_domain() -> dict[str, Any]:
    """Per ogni modello/scope: hit-rate e Brier per categoria (patch utente)."""
    return await reliability_table(min_sample=5)


async def failure_detection(*, mode: str, lookback_days: int = 14) -> dict[str, Any]:
    """Model drift, strategy decay, execution deterioration, source deterioration, API latency."""
    since = utcnow() - timedelta(days=lookback_days)
    half = utcnow() - timedelta(days=lookback_days / 2)
    async with session_scope() as session:
        repo = Repository(session)
        preds = await repo.resolved_predictions(limit=20000)
        positions = list(await repo.closed_positions(since=since, mode=mode, limit=10000))
        orders = [o for o in await repo.recent_orders(limit=5000) if o.mode == mode and o.created_at >= since and o.purpose == "OPEN"]
        news = await repo.recent_news(minutes=lookback_days * 24 * 60, limit=5000)
    flags: list[dict[str, Any]] = []

    recent = [(p.predicted_probability, p.realized_outcome) for p in preds if p.resolved_at and p.resolved_at >= half and p.realized_outcome is not None]
    older = [(p.predicted_probability, p.realized_outcome) for p in preds if p.resolved_at and since <= p.resolved_at < half and p.realized_outcome is not None]
    if len(recent) >= 10 and len(older) >= 10:
        b_recent, b_old = calibration_metrics(recent)["brier"], calibration_metrics(older)["brier"]
        if b_recent > b_old * 1.25:
            flags.append({"type": "model_drift", "detail": f"Brier {b_old:.3f} -> {b_recent:.3f}"})

    pnl_recent = [float(p.realized_pnl or 0) for p in positions if p.closed_at and p.closed_at >= half]
    pnl_old = [float(p.realized_pnl or 0) for p in positions if p.closed_at and p.closed_at < half]
    if len(pnl_recent) >= 8 and len(pnl_old) >= 8 and sum(pnl_recent) < 0 < sum(pnl_old):
        flags.append({"type": "strategy_decay", "detail": f"pnl {sum(pnl_old):.2f} -> {sum(pnl_recent):.2f}"})

    slips = [float(o.slippage_pct) for o in orders if o.slippage_pct is not None]
    rejected = sum(1 for o in orders if o.status == "REJECTED")
    if slips and sum(slips) / len(slips) > 0.0005:
        flags.append({"type": "execution_deterioration", "detail": f"slippage medio {sum(slips) / len(slips):.5f}"})
    if orders and rejected / len(orders) > 0.3:
        flags.append({"type": "execution_deterioration", "detail": f"{rejected}/{len(orders)} ordini rifiutati"})

    by_source: dict[str, list[int]] = {}
    for item in news:
        by_source.setdefault(item.source_name, []).append(1 if item.published_at and item.published_at >= half else 0)
    for source, marks in by_source.items():
        if len(marks) >= 20 and sum(marks) == 0:
            flags.append({"type": "news_source_deterioration", "detail": f"{source}: nessuna news nell'ultima meta periodo"})

    return {"lookback_days": lookback_days, "flags": flags, "n_predictions": len(preds), "n_positions": len(positions), "n_orders": len(orders)}
