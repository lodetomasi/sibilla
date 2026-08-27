"""Riconciliazione posizioni/conto con IG (patch sez. 24/41; sez. 27 kill switch)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.enums import KillSwitchReason, PositionStatus
from core.logging import get_logger
from core.repository import Repository
from core.schemas import AccountState, BrokerPosition
from execution.ig.positions import parse_account_state, parse_broker_position

log = get_logger("execution.ig.reconciliation")


@dataclass
class ReconciliationReport:
    ts: Any
    broker_positions: int
    local_positions: int
    matched: list[str] = field(default_factory=list)
    missing_at_broker: list[str] = field(default_factory=list)  # locali OPEN ma assenti su IG
    unknown_at_broker: list[str] = field(default_factory=list)  # su IG ma non tracciate
    size_mismatch: list[dict[str, Any]] = field(default_factory=list)
    account: AccountState | None = None
    balance_mismatch: float | None = None

    @property
    def clean(self) -> bool:
        return not (self.missing_at_broker or self.unknown_at_broker or self.size_mismatch)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat() if hasattr(self.ts, "isoformat") else str(self.ts),
            "broker_positions": self.broker_positions,
            "local_positions": self.local_positions,
            "matched": self.matched,
            "missing_at_broker": self.missing_at_broker,
            "unknown_at_broker": self.unknown_at_broker,
            "size_mismatch": self.size_mismatch,
            "balance_mismatch": self.balance_mismatch,
            "clean": self.clean,
        }


class Reconciler:
    def __init__(self, client: Any, *, mode: str, kill_switch: Any | None = None, expected_equity: float | None = None):
        self.client = client
        self.mode = mode
        self.kill_switch = kill_switch
        self.expected_equity = expected_equity

    async def broker_positions(self) -> list[BrokerPosition]:
        return [parse_broker_position(raw) for raw in await self.client.get_positions()]

    async def account_state(self) -> AccountState:
        session = await self.client.authenticate()
        return parse_account_state(await self.client.get_account(), account_id=session.account_id)

    async def run(self) -> ReconciliationReport:
        broker = {p.deal_id: p for p in await self.broker_positions()}
        account = await self.account_state()
        async with session_scope() as db:
            repo = Repository(db)
            local = list(await repo.open_positions(self.mode))
            report = ReconciliationReport(ts=utcnow(), broker_positions=len(broker), local_positions=len(local), account=account)
            local_by_deal = {p.deal_id: p for p in local if p.deal_id}
            for deal_id, position in local_by_deal.items():
                if deal_id in broker:
                    bp = broker[deal_id]
                    if abs(float(position.size) - bp.size) > 1e-6:
                        report.size_mismatch.append({"deal_id": deal_id, "local": float(position.size), "broker": bp.size})
                        await repo.update_position(position.trade_id, size=bp.size, reconciliation_status="SIZE_MISMATCH", reconciled_at=utcnow())
                    else:
                        report.matched.append(deal_id)
                        await repo.update_position(
                            position.trade_id,
                            current_price=(bp.bid if position.direction == "BUY" else bp.offer) or position.current_price,
                            stop_level=bp.stop_level or position.stop_level,
                            limit_level=bp.limit_level or position.limit_level,
                            reconciliation_status="OK",
                            reconciled_at=utcnow(),
                            status=PositionStatus.OPEN.value if position.status == PositionStatus.PENDING_CONFIRMATION.value else position.status,
                        )
                else:
                    if position.status in (PositionStatus.OPEN.value, PositionStatus.REDUCED.value):
                        # chiusa dal broker (stop/limit colpiti) senza che l'abbiamo vista
                        report.missing_at_broker.append(deal_id)
                        await repo.update_position(position.trade_id, reconciliation_status="CLOSED_AT_BROKER", reconciled_at=utcnow())
            for deal_id in broker:
                if deal_id not in local_by_deal:
                    report.unknown_at_broker.append(deal_id)
            if self.expected_equity is not None:
                report.balance_mismatch = account.equity - self.expected_equity
        if not report.clean and self.kill_switch is not None:
            if report.unknown_at_broker or report.size_mismatch:
                await self.kill_switch.trigger(KillSwitchReason.RECONCILIATION_MISMATCH, by="reconciler", **report.as_dict())
        if report.balance_mismatch is not None and self.kill_switch is not None and abs(report.balance_mismatch) > max(50.0, 0.05 * account.equity):
            await self.kill_switch.trigger(KillSwitchReason.BALANCE_MISMATCH, by="reconciler", mismatch=report.balance_mismatch)
        log.info("reconciliation.done", **{k: v for k, v in report.as_dict().items() if k != "ts"})
        return report
