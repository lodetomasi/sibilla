"""Kill switch (sez. 27, 71): blocco immediato dell'esecuzione."""
from __future__ import annotations

from typing import Any

from core.audit import audit
from core.bus import emit
from core.clock import utcnow
from core.db import session_scope
from core.enums import EventType, KillSwitchReason, SystemState
from core.errors import KillSwitchActive
from core.logging import get_logger
from core.repository import Repository

log = get_logger("risk.kill_switch")

STATE_KEY = "system_state"


class KillSwitch:
    """Stato in memoria + persistenza; `guard()` solleva se l'esecuzione e' bloccata."""

    def __init__(self) -> None:
        self._active: KillSwitchReason | None = None
        self._details: dict[str, Any] = {}
        self._state: SystemState = SystemState.RUNNING
        self.triggered_at = None

    @property
    def active(self) -> bool:
        return self._active is not None or self._state in (SystemState.STOPPED, SystemState.KILLED)

    @property
    def paused(self) -> bool:
        return self._state is SystemState.PAUSED

    @property
    def reason(self) -> KillSwitchReason | None:
        return self._active

    @property
    def state(self) -> SystemState:
        return self._state

    def guard(self) -> None:
        if self.active:
            raise KillSwitchActive(f"kill switch attivo: {self._active or self._state}")
        if self.paused:
            raise KillSwitchActive("sistema in PAUSE: nessuna nuova esecuzione")

    async def trigger(self, reason: KillSwitchReason, *, by: str = "system", **details: Any) -> None:
        if self._active is reason:
            return
        self._active = reason
        self._details = details
        self._state = SystemState.KILLED
        self.triggered_at = utcnow()
        log.error("kill_switch.triggered", reason=reason.value, by=by, **{k: str(v)[:120] for k, v in details.items()})
        try:
            async with session_scope() as session:
                repo = Repository(session)
                await repo.add_kill_switch_event(reason=reason.value, triggered_by=by, details=details)
                await repo.set_state(STATE_KEY, SystemState.KILLED.value, by=by, reason=reason.value)
        except Exception as exc:  # noqa: BLE001 - il blocco vale anche se il DB non risponde
            log.error("kill_switch.persist_failed", error=str(exc)[:160])
        await audit("kill_switch_triggered", actor=by, entity="system", after={"reason": reason.value, **details})
        await emit(EventType.KILL_SWITCH_TRIGGERED, {"reason": reason.value, "by": by, **details}, source="kill_switch")

    async def clear(self, *, by: str) -> None:
        """Solo un operatore umano riattiva il sistema (sez. 71)."""
        if by.lower().startswith(("llm", "agent", "system")):
            raise KillSwitchActive("il kill switch puo essere riattivato solo da un operatore umano")
        self._active = None
        self._details = {}
        self._state = SystemState.RUNNING
        async with session_scope() as session:
            repo = Repository(session)
            await repo.clear_kill_switch(by)
            await repo.set_state(STATE_KEY, SystemState.RUNNING.value, by=by)
        await audit("kill_switch_cleared", actor=by, entity="system")
        log.info("kill_switch.cleared", by=by)

    async def set_state(self, state: SystemState, *, by: str) -> None:
        """PAUSE / STOP / RUNNING dalla dashboard (sez. 71)."""
        self._state = state
        if state is SystemState.RUNNING:
            self._active = None
        async with session_scope() as session:
            await Repository(session).set_state(STATE_KEY, state.value, by=by)
        await audit("execution_mode_changed", actor=by, entity="system", after={"state": state.value})

    async def load(self) -> None:
        try:
            async with session_scope() as session:
                repo = Repository(session)
                active = await repo.active_kill_switch()
                state = await repo.get_state(STATE_KEY, SystemState.RUNNING.value)
            if active:
                self._active = KillSwitchReason(active.reason)
                self._details = active.details or {}
            self._state = SystemState(state or SystemState.RUNNING.value)
        except Exception as exc:  # noqa: BLE001
            log.warning("kill_switch.load_failed", error=str(exc)[:120])

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "active": self.active,
            "reason": self._active.value if self._active else None,
            "details": self._details,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
        }


_kill_switch: KillSwitch | None = None


def get_kill_switch() -> KillSwitch:
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = KillSwitch()
    return _kill_switch


def set_kill_switch(ks: KillSwitch | None) -> None:
    global _kill_switch
    _kill_switch = ks
