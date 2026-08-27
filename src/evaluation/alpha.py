"""Post-signal alpha (patch sez. 35) e funzione obiettivo (sez. 34: risk-adjusted realized alpha)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.db import session_scope
from core.repository import Repository
from quant.features import HORIZONS_S


async def post_signal_alpha_report(*, mode: str | None = None, since: datetime | None = None) -> dict[str, Any]:
    async with session_scope() as session:
        entries = list(await Repository(session).journal_entries(mode=mode, since=since, limit=10000))
    by_horizon: dict[str, list[float]] = {h: [] for h in HORIZONS_S}
    by_strategy: dict[str, dict[str, list[float]]] = {}
    for entry in entries:
        psa = entry.post_signal_alpha or {}
        for horizon, value in psa.items():
            if value is None or horizon not in by_horizon:
                continue
            by_horizon[horizon].append(float(value))
            by_strategy.setdefault(entry.strategy_id or "unknown", {}).setdefault(horizon, []).append(float(value))

    def summarize(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"n": 0, "mean": None, "median": None, "hit_rate": None}
        ordered = sorted(values)
        return {"n": len(values), "mean": round(sum(values) / len(values), 6), "median": round(ordered[len(ordered) // 2], 6), "hit_rate": round(sum(1 for v in values if v > 0) / len(values), 3)}

    return {
        "overall": {h: summarize(v) for h, v in by_horizon.items()},
        "by_strategy": {s: {h: summarize(v) for h, v in hs.items()} for s, hs in by_strategy.items()},
        "n_trades": len(entries),
        "interpretation": "post-signal alpha > 0 e crescente con l'orizzonte = informazione anticipatoria reale; ~0 = il segnale arriva dopo il repricing",
    }


def risk_adjusted_alpha(pnls: list[float], risks: list[float]) -> dict[str, float | None]:
    """R-multipli: pnl / rischio allo stop. La funzione obiettivo e' la media dei R realizzati."""
    r_multiples = [p / r for p, r in zip(pnls, risks, strict=False) if r and r > 0]
    if not r_multiples:
        return {"n": 0, "mean_r": None, "sum_r": None, "win_rate": None}
    return {"n": len(r_multiples), "mean_r": round(sum(r_multiples) / len(r_multiples), 3), "sum_r": round(sum(r_multiples), 3), "win_rate": round(sum(1 for r in r_multiples if r > 0) / len(r_multiples), 3), "best_r": round(max(r_multiples), 3), "worst_r": round(min(r_multiples), 3)}
