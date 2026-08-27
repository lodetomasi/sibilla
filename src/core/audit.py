"""Audit trail (sez. 53) e tracciamento costi (sez. 40)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.logging import get_logger, scrub_value
from core.repository import Repository

log = get_logger("core.audit")

AUDITED_ACTIONS = (
    "risk_config_changed",
    "strategy_changed",
    "strategy_status_changed",
    "prompt_changed",
    "model_changed",
    "api_key_changed",
    "execution_mode_changed",
    "autonomy_level_changed",
    "kill_switch_triggered",
    "kill_switch_cleared",
    "manual_override",
    "position_closed_manually",
    "signal_weights_updated",
    "source_reliability_updated",
    "wallet_ranking_updated",
    "calibration_updated",
)


async def audit(
    action: str,
    *,
    actor: str = "system",
    entity: str | None = None,
    entity_id: str | Any = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """Registra un cambiamento. I valori sono ripuliti dai secret prima di salvarli."""
    payload_before = scrub_value(before or {})
    payload_after = scrub_value(after or {})
    async with session_scope() as session:
        await Repository(session).add_audit(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=payload_before,
            after=payload_after,
            note=note,
        )
    log.info("audit", action=action, actor=actor, entity=entity, entity_id=entity_id)


async def record_cost(
    kind: str, amount_usd: float, *, provider: str | None = None, units: float = 0.0, **details: Any
) -> None:
    """Sez. 40 - costi per LLM, news API, market API, server, storage, execution."""
    async with session_scope() as session:
        await Repository(session).add_cost(
            kind=kind, provider=provider, amount_usd=amount_usd, units=units, details=details
        )


async def cost_summary(days: int = 30) -> dict[str, float]:
    since = utcnow() - timedelta(days=days)
    async with session_scope() as session:
        costs = await Repository(session).costs_since(since)
    total = sum(costs.values())
    return {**costs, "total": total}


async def profit_after_information_cost(days: int = 30) -> dict[str, float]:
    """Sez. 40 - profit_after_information_cost."""
    since = utcnow() - timedelta(days=days)
    async with session_scope() as session:
        repo = Repository(session)
        costs = await repo.costs_since(since)
        entries = await repo.journal_entries(since=since)
    gross = sum(float(e.pnl or 0.0) for e in entries)
    total_cost = sum(costs.values())
    return {
        "gross_pnl": gross,
        "information_cost": total_cost,
        "profit_after_information_cost": gross - total_cost,
        "trades": len(entries),
    }
