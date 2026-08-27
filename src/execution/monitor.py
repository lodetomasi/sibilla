"""Position Monitor (patch sez. 17/18/38): time stop, thesis invalidation, mark-to-market.

Gira periodicamente:
  - aggiorna current_price/unrealized P&L delle posizioni aperte;
  - PAPER: applica stop/target sui prezzi live;
  - time stop: max_holding_until superato -> CLOSE;
  - thesis invalidation: condizioni verificate dal monitor (prezzo torna sotto il
    livello pre-evento, fonte ritrattata, cross-asset contro) -> THESIS_INVALIDATED -> CLOSE;
  - post-signal alpha (patch sez. 35) al momento della chiusura.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.bus import emit
from core.clock import utcnow
from core.db import session_scope
from core.enums import Direction, EventType, ExecutionMode, ExitReason, PositionStatus
from core.logging import get_logger
from core.pricing import pnl_money
from core.repository import Repository
from core.schemas import Quote
from execution.engine import ExecutionEngine
from quant.features import returns_after

log = get_logger("execution.monitor")


class PositionMonitor:
    def __init__(self, engine: ExecutionEngine):
        self.engine = engine
        self.prices = engine.prices
        self.mode = engine.mode
        self._invalidation_checks: list[Any] = []

    def add_invalidation_check(self, fn: Any) -> None:
        """fn(position, quote) -> str | None (motivo) ; async o sync."""
        self._invalidation_checks.append(fn)

    async def tick(self) -> dict[str, Any]:
        async with session_scope() as session:
            rows = list(await Repository(session).open_positions(self.mode.value))
        if not rows:
            return {"open": 0, "closed": 0}
        epics = sorted({row.epic for row in rows})
        quotes = await self.prices.quotes(epics, max_age_s=self.prices.max_staleness_s)
        closed = 0

        # PAPER: stop/target sui prezzi live
        if self.mode is ExecutionMode.PAPER:
            for result in await self.engine.paper.mark_to_market(quotes):
                deal_id = result.deal_id
                for row in rows:
                    if row.deal_id == deal_id:
                        reason = ExitReason.STOP_HIT if result.raw.get("reason") == "STOP_HIT" else ExitReason.TARGET_HIT
                        await self._finalize_paper_close(row, result, reason)
                        closed += 1

        now = utcnow()
        for row in rows:
            if row.status == PositionStatus.CLOSED.value:
                continue
            quote = quotes.get(row.epic)
            if quote is None:
                continue
            direction = Direction.parse(row.direction)
            exit_px = quote.exit_price_for(direction)
            unrealized = pnl_money(float(row.entry_price), exit_px, row.direction, float(row.size), float(row.value_per_point))
            async with session_scope() as session:
                await Repository(session).update_position(row.trade_id, current_price=exit_px, unrealized_pnl=unrealized)

            # SHADOW: stop/target teorici
            if self.mode is ExecutionMode.SHADOW and row.stop_level is not None:
                stop_hit = exit_px <= row.stop_level if direction is Direction.BUY else exit_px >= row.stop_level
                limit_hit = row.limit_level is not None and (exit_px >= row.limit_level if direction is Direction.BUY else exit_px <= row.limit_level)
                if stop_hit or limit_hit:
                    await self._close(row, ExitReason.STOP_HIT if stop_hit else ExitReason.TARGET_HIT, quote)
                    closed += 1
                    continue

            # time stop (patch sez. 17)
            if row.max_holding_until and now >= row.max_holding_until:
                await self._close(row, ExitReason.TIME_STOP, quote)
                closed += 1
                continue

            # thesis invalidation (patch sez. 18)
            reason = await self._check_invalidation(row, quote)
            if reason:
                await emit(EventType.THESIS_INVALIDATED, {"trade_id": row.trade_id, "epic": row.epic, "reason": reason}, source="monitor")
                await self._close(row, ExitReason.THESIS_INVALIDATED, quote)
                closed += 1
        return {"open": len(rows) - closed, "closed": closed}

    async def _check_invalidation(self, row: Any, quote: Quote) -> str | None:
        criteria = row.exit_criteria or {}
        direction = Direction.parse(row.direction)
        pre_event = criteria.get("pre_event_price")
        if pre_event:
            px = quote.exit_price_for(direction)
            if (direction is Direction.BUY and px < float(pre_event)) or (direction is Direction.SELL and px > float(pre_event)):
                return "price returned through pre-event level"
        for fn in self._invalidation_checks:
            try:
                result = fn(row, quote)
                if hasattr(result, "__await__"):
                    result = await result
                if result:
                    return str(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("monitor.invalidation_check_failed", error=str(exc)[:120])
        return None

    async def _close(self, row: Any, reason: ExitReason, quote: Quote) -> None:
        try:
            await self.engine.close_position(row.trade_id, reason=reason, by="monitor", quote=quote)
            await self._record_post_signal_alpha(row)
        except Exception as exc:  # noqa: BLE001
            log.error("monitor.close_failed", trade_id=row.trade_id, error=str(exc)[:200])

    async def _finalize_paper_close(self, row: Any, result: Any, reason: ExitReason) -> None:
        pnl = float(result.raw.get("pnl", 0.0))
        async with session_scope() as session:
            repo = Repository(session)
            await repo.update_position(row.trade_id, status=PositionStatus.CLOSED.value, closed_at=utcnow(), exit_price=result.fill_price, exit_reason=reason.value, realized_pnl=pnl, unrealized_pnl=0.0)
            await repo.update_journal_entry(row.trade_id, exit_price=result.fill_price, exit_reason=reason.value, pnl=pnl, outcome=f"CLOSED_{reason.value}")
        await emit(EventType.POSITION_CLOSED, {"trade_id": row.trade_id, "epic": row.epic, "exit": result.fill_price, "pnl": round(pnl, 2), "reason": reason.value, "mode": self.mode.value}, source="monitor")
        await self._record_post_signal_alpha(row)

    async def _record_post_signal_alpha(self, row: Any) -> None:
        """Patch sez. 35: return a 5s..1h dall'apertura vs entry, nel verso del trade."""
        try:
            series = await self.prices.price_series(row.epic, since=row.opened_at - timedelta(seconds=5), until=utcnow())
            raw = returns_after(series, row.opened_at, float(row.entry_price), now=utcnow())
            sign = Direction.parse(row.direction).sign
            signed = {k: (v * sign if v is not None else None) for k, v in raw.items()}
            async with session_scope() as session:
                await Repository(session).update_journal_entry(row.trade_id, post_signal_alpha=signed)
        except Exception as exc:  # noqa: BLE001
            log.warning("monitor.post_signal_alpha_failed", trade_id=row.trade_id, error=str(exc)[:120])
