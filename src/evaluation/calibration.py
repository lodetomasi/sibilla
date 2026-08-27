"""Calibrazione (sez. 37): Brier, log loss, ECE, curva; adjustment appreso per scope."""
from __future__ import annotations

import math
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.repository import Repository

BUCKETS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def calibration_metrics(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    if not pairs:
        return {"n": 0}
    n = len(pairs)
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    eps = 1e-6
    log_loss = -sum(o * math.log(max(p, eps)) + (1 - o) * math.log(max(1 - p, eps)) for p, o in pairs) / n
    curve = []
    ece = 0.0
    for lo, hi in BUCKETS:
        bucket = [(p, o) for p, o in pairs if lo <= p < hi]
        if not bucket:
            continue
        pred = sum(p for p, _ in bucket) / len(bucket)
        obs = sum(o for _, o in bucket) / len(bucket)
        ece += abs(pred - obs) * len(bucket) / n
        curve.append({"bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}", "predicted_mean": round(pred, 3), "observed_rate": round(obs, 3), "n": len(bucket), "adjustment": round(obs - pred, 3)})
    accuracy = sum(1 for p, o in pairs if (p >= 0.5) == (o == 1)) / n
    tp = sum(1 for p, o in pairs if p >= 0.5 and o == 1)
    fp = sum(1 for p, o in pairs if p >= 0.5 and o == 0)
    fn = sum(1 for p, o in pairs if p < 0.5 and o == 1)
    return {"n": n, "brier": round(brier, 4), "log_loss": round(log_loss, 4), "ece": round(ece, 4), "accuracy": round(accuracy, 3), "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None, "curve": curve}


async def update_calibration(scope: str, *, category: str | None = None) -> dict[str, Any]:
    async with session_scope() as session:
        repo = Repository(session)
        rows = await repo.resolved_predictions(scope=scope, category=category)
        pairs = [(float(r.predicted_probability), int(r.realized_outcome)) for r in rows if r.realized_outcome is not None]
        metrics = calibration_metrics(pairs)
        as_of = utcnow()
        records = [{"scope": scope if not category else f"{scope}:{category}", "bucket": c["bucket"], "as_of": as_of, "predicted_mean": c["predicted_mean"], "observed_rate": c["observed_rate"], "sample_size": c["n"], "adjustment": c["adjustment"]} for c in metrics.get("curve", [])]
        if records:
            await repo.save_calibration(records)
        await repo.add_evaluation(kind="calibration", scope=scope if not category else f"{scope}:{category}", metrics=metrics)
    return metrics


async def calibrated_probability(scope: str, probability: float) -> float:
    """Applica l'adjustment del bucket (shrinkage se il campione e' piccolo)."""
    async with session_scope() as session:
        records = await Repository(session).calibration_for(scope)
    for record in records:
        lo, hi = (float(x) for x in record.bucket.split("-"))
        if lo <= probability < (hi if hi < 1.0 else 1.01):
            shrink = record.sample_size / (record.sample_size + 20)
            return max(0.01, min(0.99, probability + record.adjustment * shrink))
    return probability
