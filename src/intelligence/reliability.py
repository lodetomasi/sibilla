"""Reliability per modello e categoria (patch utente: "impara empiricamente a chi dare peso").

Ogni tesi degli analisti e la decisione del PM vengono registrate come Prediction
con scope = ruolo. Quando il post-signal alpha e' disponibile si risolve la
prediction (direzione corretta a 15m/1h) e si aggiornano hit-rate e Brier per
(modello, categoria). Il PM riceve la tabella come input.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.enums import Direction
from core.repository import Repository
from quant.features import returns_after

ANALYST_SCOPES = ("causal_analyst", "independent_analyst", "contrarian_agent", "adversarial_red_team", "final_portfolio_manager", "investigator")


async def record_model_prediction(*, scope: str, event_id: str, trade_id: str | None, epic: str | None, direction: Direction | None, category: str, probability: float, expected_move_pct: float | None, confidence: float | None, details: dict[str, Any] | None = None) -> int:
    async with session_scope() as session:
        prediction = await Repository(session).add_prediction(scope=scope, event_id=event_id, trade_id=trade_id, epic=epic, direction=direction.value if direction else None, category=category, predicted_probability=probability, expected_move_pct=expected_move_pct, confidence=confidence, details=details or {})
        return prediction.id


async def resolve_pending_predictions(prices: Any, *, horizon: str = "15m", min_age_s: int = 900) -> int:
    """Risolve le prediction con abbastanza storia: hit = return nel verso previsto > 0."""
    resolved = 0
    async with session_scope() as session:
        pending = list(await Repository(session).unresolved_predictions())
    for prediction in pending:
        if prediction.epic is None or prediction.direction is None:
            continue
        if (utcnow() - prediction.ts).total_seconds() < min_age_s:
            continue
        series = await prices.price_series(prediction.epic, since=prediction.ts - timedelta(seconds=5), until=prediction.ts + timedelta(hours=1, minutes=5))
        if len(series) < 2:
            continue
        entry = next((v for ts, v in series if ts >= prediction.ts), series[0][1])
        rets = returns_after(series, prediction.ts, entry, now=utcnow())
        r = rets.get(horizon)
        if r is None:
            continue
        sign = Direction.parse(prediction.direction).sign
        realized = r * sign
        async with session_scope() as session:
            await Repository(session).resolve_prediction(prediction.id, int(realized > 0), realized_move_pct=realized)
        resolved += 1
    return resolved


async def reliability_table(*, min_sample: int = 5) -> dict[str, dict[str, dict[str, float]]]:
    """{scope: {category: {hit_rate, brier, n}}} calcolata sulle prediction risolte."""
    table: dict[str, dict[str, dict[str, float]]] = {}
    async with session_scope() as session:
        rows = await Repository(session).resolved_predictions(limit=20000)
    buckets: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for row in rows:
        if row.scope not in ANALYST_SCOPES or row.realized_outcome is None:
            continue
        for key in ((row.scope, row.category or "other"), (row.scope, "ALL")):
            buckets.setdefault(key, []).append((row.predicted_probability, int(row.realized_outcome)))
    for (scope, category), obs in buckets.items():
        if len(obs) < min_sample:
            continue
        hits = sum(o for _, o in obs)
        brier = sum((p - o) ** 2 for p, o in obs) / len(obs)
        table.setdefault(scope, {})[category] = {"hit_rate": round(hits / len(obs), 3), "brier": round(brier, 4), "n": len(obs)}
    return table


def weights_for_category(table: dict[str, dict[str, dict[str, float]]], category: str) -> dict[str, float]:
    """Pesi suggeriti al PM (informativi: il PM decide, non vota)."""
    weights: dict[str, float] = {}
    for scope, cats in table.items():
        stats = cats.get(category) or cats.get("ALL")
        if not stats:
            continue
        weights[scope] = round(max(0.1, stats["hit_rate"] - 0.5 + (1 - stats["brier"])) , 3)
    return weights
