"""Hard limits: accesso in sola lettura per tutto il sistema (sez. 25, patch sez. 13/27).

Le modifiche avvengono solo tramite `update_limits_human()` con attore umano,
audit trail e mai da codice invocato dagli agenti.
"""
from __future__ import annotations

from typing import Any

from core.audit import audit
from core.config import RiskLimits, get_settings
from core.errors import RiskViolation
from core.logging import get_logger

log = get_logger("risk.limits")

_override: RiskLimits | None = None
_LLM_ACTORS = {"llm", "agent", "analyst", "critic", "portfolio_manager", "judge", "system:llm"}


def current_limits() -> RiskLimits:
    return _override or get_settings().risk


async def update_limits_human(actor: str, changes: dict[str, Any], *, note: str | None = None) -> RiskLimits:
    """Unico canale di modifica; rifiuta attori non umani (hard rule 1)."""
    if not actor or actor.lower() in _LLM_ACTORS or actor.lower().startswith("llm"):
        raise RiskViolation("i limiti di rischio possono essere modificati solo da un operatore umano", code="LLM_RISK_EDIT")
    global _override
    before = current_limits()
    after = before.model_copy(update=changes)  # ri-valida i vincoli (frozen -> nuova istanza)
    RiskLimits.model_validate(after.model_dump())
    _override = after
    await audit(
        "risk_config_changed",
        actor=actor,
        entity="risk_limits",
        before=before.model_dump(),
        after=after.model_dump(),
        note=note,
    )
    log.info("risk.limits.updated", actor=actor, changes=list(changes))
    return after


def reset_limits_override() -> None:
    global _override
    _override = None
