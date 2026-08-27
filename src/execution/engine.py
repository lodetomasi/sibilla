"""Execution Engine (sez. 28-31, patch sez. 22-24, 26).

Unico punto che invia ordini. Riceve SOLO OrderRequest gia approvati dal Risk
Engine (RiskDecision.approved) e:
  SHADOW  -> registra l'ordine che avrebbe eseguito, nessun fill;
  PAPER   -> PaperBroker su prezzi live reali;
  DEMO/LIVE -> IG con conferma obbligatoria e riconciliazione.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from core.bus import emit
from core.clock import utcnow
from core.config import Settings, get_settings
from core.db import session_scope
from core.enums import Direction, EventType, ExecutionMode, ExitReason, OrderStatus, PositionStatus
from core.errors import ExecutionError, RiskViolation
from core.logging import get_logger
from core.pricing import margin_required, notional, pnl_money
from core.repository import Repository
from core.schemas import AccountState, OrderRequest, OrderResult, Quote, RiskDecision, TradeProposal
from execution.paper import PaperBroker
from market.instrument_registry import InstrumentRegistry, get_registry
from market.prices import PriceService, get_price_service
from risk.engine import PortfolioContext, RiskEngine
from risk.kill_switch import KillSwitch, get_kill_switch

log = get_logger("execution.engine")


class ExecutionEngine:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        registry: InstrumentRegistry | None = None,
        prices: PriceService | None = None,
        paper: PaperBroker | None = None,
        ig_gateway: Any | None = None,
        ig_client: Any | None = None,
        kill_switch: KillSwitch | None = None,
    ):
        self.settings = settings or get_settings()
        self.mode = self.settings.execution_mode
        self.registry = registry or get_registry()
        self.prices = prices or get_price_service()
        self.paper = paper or PaperBroker()
        self.ig_gateway = ig_gateway
        self.ig_client = ig_client
        self.kill_switch = kill_switch or get_kill_switch()
        self.risk_engine = RiskEngine(registry=self.registry, kill_switch=self.kill_switch, mode=self.mode)
        self._account_cache: AccountState | None = None

    # ------------------------------------------------------------- account
    async def account_state(self) -> AccountState:
        if self.mode.sends_orders_to_broker and self.ig_client is not None:
            from execution.ig.positions import parse_account_state

            session = await self.ig_client.authenticate()
            raw = await self.ig_client.get_account()
            self._account_cache = parse_account_state(raw, account_id=session.account_id)
            return self._account_cache
        return self.paper.account_state()

    async def portfolio_context(self) -> PortfolioContext:
        account = await self.account_state()
        async with session_scope() as session:
            repo = Repository(session)
            rows = list(await repo.open_positions(self.mode.value))
            today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            pnl_today = await repo.realized_pnl_since(today, self.mode.value)
            pnl_week = await repo.realized_pnl_since(RiskEngine.week_start(), self.mode.value)
            trades_today = await repo.orders_today()
            streak = await repo.rejected_orders_streak()
            history = await repo.portfolio_history(since=RiskEngine.week_start(), mode=self.mode.value)
        peak = max([float(h.equity) for h in history] + [account.equity]) if history else account.equity
        exposures = self.risk_engine.exposure_from_positions(rows, self.registry)
        by_event: dict[str, float] = {}
        for row in rows:
            if row.event_id:
                by_event[row.event_id] = by_event.get(row.event_id, 0.0) + float(row.risk_eur or 0.0)
        return PortfolioContext(
            account=account, open_positions=exposures, realized_pnl_today=pnl_today, realized_pnl_week=pnl_week,
            peak_equity_week=peak, trades_today=trades_today, rejected_streak=streak, positions_for_event=by_event,
        )

    # ---------------------------------------------------------------- risk
    async def assess(self, proposal: TradeProposal) -> RiskDecision:
        context = await self.portfolio_context()
        return self.risk_engine.evaluate(proposal, context)

    # -------------------------------------------------------------- submit
    async def submit(self, proposal: TradeProposal, decision: RiskDecision, *, quote: Quote | None = None) -> OrderResult:
        """Invia l'ordine approvato. Rifiuta qualsiasi proposta non approvata (hard rule 2)."""
        if not decision.approved or decision.size <= 0:
            raise RiskViolation("ordine non approvato dal Risk Engine", code="NOT_APPROVED")
        self.kill_switch.guard()
        live_quote = quote or await self.prices.quote(proposal.epic)
        self.prices.require_fresh(live_quote)
        if not live_quote.market_status.tradeable:
            raise ExecutionError(f"mercato {proposal.epic} non TRADEABLE: {live_quote.market_status.value}")

        client_order_id = f"ATS{uuid.uuid4().hex[:20]}"
        request = OrderRequest(
            client_order_id=client_order_id,
            trade_id=proposal.trade_id,
            epic=proposal.epic,
            direction=proposal.direction,
            size=decision.size,
            entry_type=proposal.entry_type,
            max_entry=decision.max_entry,
            reference_price=live_quote.price_for(proposal.direction),
            stop_distance=decision.stop_distance,
            limit_distance=decision.limit_distance,
            currency_code=proposal.instrument.currency,
            expiry=proposal.instrument.expiry,
            time_horizon_seconds=proposal.time_horizon_seconds,
            invalidation_conditions=proposal.invalidation_conditions,
            reason_code=proposal.reason_code,
            reason="; ".join(proposal.explanation)[:1000],
            strategy_id=proposal.strategy_id,
            event_id=proposal.event_id,
            risk_eur=decision.risk_eur,
        )
        await self._record_order(request, proposal, decision, status=OrderStatus.SUBMITTED)
        await emit(EventType.ORDER_SUBMITTED, {"trade_id": proposal.trade_id, "epic": proposal.epic, "direction": proposal.direction.value, "size": decision.size, "mode": self.mode.value}, source="execution")

        try:
            if self.mode is ExecutionMode.SHADOW:
                result = self._shadow_result(request, live_quote)
            elif self.mode is ExecutionMode.PAPER:
                self.paper.update_quote(live_quote)
                result = await self.paper.open(request, proposal.instrument, live_quote)
            else:
                if self.ig_gateway is None:
                    raise ExecutionError("gateway IG non configurato per la modalita corrente")
                result = await self.ig_gateway.open(request, currency=proposal.instrument.currency)
        except Exception as exc:
            await self._update_order(client_order_id, status=OrderStatus.REJECTED, error=str(exc)[:500])
            await emit(EventType.ORDER_REJECTED, {"trade_id": proposal.trade_id, "error": str(exc)[:300]}, source="execution")
            raise

        await self._finalize_open(request, proposal, decision, result, live_quote)
        return result

    def _shadow_result(self, request: OrderRequest, quote: Quote) -> OrderResult:
        """SHADOW (sez. 31): decisione reale, nessun ordine; si registra il fill teorico."""
        return OrderResult(
            client_order_id=request.client_order_id,
            deal_reference=f"SHADOW-{request.client_order_id}",
            deal_id=f"SHADOW-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.FILLED.value,
            filled_size=request.size,
            fill_price=request.reference_price,
            requested_size=request.size,
            slippage_pct=0.0,
            timings={"order_submission_ts": utcnow().timestamp(), "fill_ts": utcnow().timestamp()},
            raw={"shadow": True, "quote_source": quote.source},
        )

    async def _finalize_open(self, request: OrderRequest, proposal: TradeProposal, decision: RiskDecision, result: OrderResult, quote: Quote) -> None:
        accepted = result.status == OrderStatus.FILLED.value and result.fill_price is not None
        await self._update_order(
            request.client_order_id,
            status=OrderStatus(result.status),
            deal_reference=result.deal_reference,
            deal_id=result.deal_id,
            fill_price=result.fill_price,
            filled_size=result.filled_size,
            slippage_pct=result.slippage_pct,
            confirmation=result.confirmation.model_dump(mode="json") if result.confirmation else {},
            timings=result.timings,
            error=result.error,
        )
        if not accepted:
            await emit(EventType.ORDER_REJECTED, {"trade_id": proposal.trade_id, "error": result.error}, source="execution")
            log.warning("execution.rejected", trade_id=proposal.trade_id, error=result.error)
            return
        fill = float(result.fill_price)  # type: ignore[arg-type]
        stop_level = result.confirmation.stop_level if result.confirmation and result.confirmation.stop_level else (
            fill - decision.stop_distance if proposal.direction is Direction.BUY else fill + decision.stop_distance
        )
        limit_level = result.confirmation.limit_level if result.confirmation and result.confirmation.limit_level else (
            (fill + decision.limit_distance if proposal.direction is Direction.BUY else fill - decision.limit_distance) if decision.limit_distance else None
        )
        instrument = proposal.instrument
        async with session_scope() as session:
            repo = Repository(session)
            await repo.add_position(
                trade_id=proposal.trade_id, deal_id=result.deal_id, deal_reference=result.deal_reference, event_id=proposal.event_id,
                strategy_id=proposal.strategy_id, venue="ig" if self.mode.sends_orders_to_broker else self.mode.value.lower(),
                environment=self.settings.ig_environment.value, epic=proposal.epic, instrument_name=instrument.name,
                asset_class=instrument.asset_class.value, currency=instrument.currency, direction=proposal.direction.value,
                size=result.filled_size, value_per_point=instrument.value_per_point, entry_price=fill, current_price=fill,
                stop_level=stop_level, limit_level=limit_level, stop_distance=decision.stop_distance, limit_distance=decision.limit_distance,
                risk_eur=decision.risk_eur, notional=notional(fill, result.filled_size, instrument.value_per_point),
                margin_required=margin_required(fill, result.filled_size, instrument.value_per_point, instrument.margin_factor),
                commission_paid=result.commission, status=PositionStatus.OPEN.value if self.mode is not ExecutionMode.SHADOW or True else PositionStatus.OPEN.value,
                opened_at=utcnow(), max_holding_until=utcnow() + timedelta(seconds=proposal.time_horizon_seconds),
                invalidation_conditions=proposal.invalidation_conditions, exit_criteria={"stop": stop_level, "limit": limit_level, "time_stop_s": proposal.time_horizon_seconds},
                factors={k.value: v for k, v in instrument.factors.items()}, reason="; ".join(proposal.explanation)[:1000], mode=self.mode.value,
                reconciliation_status="OK" if not self.mode.sends_orders_to_broker else "PENDING",
            )
            order = await repo.get_order(request.client_order_id)
            if order:
                await repo.add_fill(order_id=order.id, price=fill, size=result.filled_size, commission=result.commission, slippage_pct=result.slippage_pct, deal_id=result.deal_id, source=quote.source)
        await emit(EventType.POSITION_OPENED, {"trade_id": proposal.trade_id, "epic": proposal.epic, "direction": proposal.direction.value, "size": result.filled_size, "fill": fill, "stop": stop_level, "limit": limit_level, "mode": self.mode.value}, source="execution")
        log.info("execution.position_opened", trade_id=proposal.trade_id, epic=proposal.epic, size=result.filled_size, fill=fill, stop=stop_level, limit=limit_level, mode=self.mode.value)

    # --------------------------------------------------------------- close
    async def close_position(self, trade_id: str, *, reason: ExitReason, by: str = "system", quote: Quote | None = None) -> OrderResult | None:
        """Chiusura (stop/target/time/thesis/kill/manual) sempre via engine (patch sez. 18)."""
        async with session_scope() as session:
            position = await Repository(session).get_position(trade_id)
        if position is None or position.status == PositionStatus.CLOSED.value:
            return None
        direction = Direction.parse(position.direction)
        live_quote = quote or await self.prices.quote(position.epic, max_age_s=60)
        client_order_id = f"CLS{uuid.uuid4().hex[:20]}"
        async with session_scope() as session:
            await Repository(session).add_order(
                client_order_id=client_order_id, trade_id=trade_id, event_id=position.event_id, strategy_id=position.strategy_id,
                venue=position.venue, environment=position.environment, epic=position.epic, direction=direction.opposite.value,
                size=float(position.size), reference_price=live_quote.exit_price_for(direction), status=OrderStatus.SUBMITTED.value,
                mode=self.mode.value, purpose="CLOSE", reason_code=reason.value, reason=f"close by {by}: {reason.value}", risk_eur=0.0,
            )
            await Repository(session).update_position(trade_id, status=PositionStatus.CLOSING.value)
        try:
            if self.mode is ExecutionMode.SHADOW or (position.deal_id or "").startswith("SHADOW"):
                exit_px = live_quote.exit_price_for(direction)
                result = OrderResult(client_order_id=client_order_id, deal_id=position.deal_id, status=OrderStatus.FILLED.value, filled_size=float(position.size), fill_price=exit_px, requested_size=float(position.size), raw={"shadow": True})
            elif self.mode is ExecutionMode.PAPER or (position.deal_id or "").startswith("PAPER"):
                self.paper.update_quote(live_quote)
                result = await self.paper.close(position.deal_id, quote=live_quote, reason=reason.value)
            else:
                if self.ig_gateway is None:
                    raise ExecutionError("gateway IG non configurato")
                result = await self.ig_gateway.close(client_order_id=client_order_id, trade_id=trade_id, deal_id=position.deal_id, epic=position.epic, direction=direction, size=float(position.size), reference_price=live_quote.exit_price_for(direction))
        except Exception as exc:
            await self._update_order(client_order_id, status=OrderStatus.REJECTED, error=str(exc)[:500])
            async with session_scope() as session:
                await Repository(session).update_position(trade_id, status=PositionStatus.OPEN.value)
            raise
        await self._finalize_close(position, result, reason, client_order_id, live_quote)
        return result

    async def _finalize_close(self, position: Any, result: OrderResult, reason: ExitReason, client_order_id: str, quote: Quote) -> None:
        await self._update_order(client_order_id, status=OrderStatus(result.status), deal_reference=result.deal_reference, deal_id=result.deal_id, fill_price=result.fill_price, filled_size=result.filled_size, confirmation=result.confirmation.model_dump(mode="json") if result.confirmation else {}, timings=result.timings, error=result.error)
        if result.status != OrderStatus.FILLED.value or result.fill_price is None:
            async with session_scope() as session:
                await Repository(session).update_position(position.trade_id, status=PositionStatus.OPEN.value)
            return
        exit_px = float(result.fill_price)
        pnl = pnl_money(float(position.entry_price), exit_px, position.direction, float(position.size), float(position.value_per_point)) - result.commission
        if result.raw.get("pnl") is not None:
            pnl = float(result.raw["pnl"])
        elif result.confirmation and result.confirmation.profit is not None:
            pnl = float(result.confirmation.profit)
        async with session_scope() as session:
            repo = Repository(session)
            await repo.update_position(position.trade_id, status=PositionStatus.CLOSED.value, closed_at=utcnow(), exit_price=exit_px, exit_reason=reason.value, realized_pnl=pnl, unrealized_pnl=0.0, current_price=exit_px, commission_paid=float(position.commission_paid or 0.0) + result.commission)
            await repo.update_journal_entry(position.trade_id, exit_price=exit_px, exit_reason=reason.value, pnl=pnl, outcome=f"CLOSED_{reason.value}")
        await emit(EventType.POSITION_CLOSED, {"trade_id": position.trade_id, "epic": position.epic, "exit": exit_px, "pnl": round(pnl, 2), "reason": reason.value, "mode": self.mode.value}, source="execution")
        log.info("execution.position_closed", trade_id=position.trade_id, epic=position.epic, exit=exit_px, pnl=round(pnl, 2), reason=reason.value)

    async def amend_position(self, trade_id: str, *, stop_level: float | None = None, limit_level: float | None = None, by: str = "system") -> bool:
        async with session_scope() as session:
            position = await Repository(session).get_position(trade_id)
        if position is None or position.status != PositionStatus.OPEN.value:
            return False
        if self.mode is ExecutionMode.PAPER or (position.deal_id or "").startswith("PAPER"):
            await self.paper.amend(position.deal_id, stop_level=stop_level, limit_level=limit_level)
        elif self.mode.sends_orders_to_broker and self.ig_gateway is not None:
            confirmation = await self.ig_gateway.amend(deal_id=position.deal_id, stop_level=stop_level, limit_level=limit_level)
            if not confirmation.accepted:
                return False
        async with session_scope() as session:
            await Repository(session).update_position(trade_id, stop_level=stop_level if stop_level is not None else position.stop_level, limit_level=limit_level if limit_level is not None else position.limit_level)
        await emit(EventType.POSITION_UPDATED, {"trade_id": trade_id, "stop": stop_level, "limit": limit_level, "by": by}, source="execution")
        return True

    async def close_all(self, *, by: str, reason: ExitReason = ExitReason.MANUAL) -> list[OrderResult]:
        async with session_scope() as session:
            rows = list(await Repository(session).open_positions(self.mode.value))
        results: list[OrderResult] = []
        for row in rows:
            try:
                result = await self.close_position(row.trade_id, reason=reason, by=by)
                if result:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                log.error("execution.close_all.failed", trade_id=row.trade_id, error=str(exc)[:200])
        return results

    # ------------------------------------------------------------ persist
    async def _record_order(self, request: OrderRequest, proposal: TradeProposal, decision: RiskDecision, *, status: OrderStatus) -> None:
        async with session_scope() as session:
            await Repository(session).add_order(
                client_order_id=request.client_order_id, trade_id=request.trade_id, event_id=request.event_id, strategy_id=request.strategy_id,
                venue="ig" if self.mode.sends_orders_to_broker else self.mode.value.lower(), environment=self.settings.ig_environment.value,
                epic=request.epic, direction=request.direction.value, entry_type=request.entry_type.value, size=request.size,
                reference_price=request.reference_price, max_entry=request.max_entry, level=request.level, stop_distance=request.stop_distance,
                limit_distance=request.limit_distance, stop_level=decision.stop_level, limit_level=decision.limit_level, risk_eur=request.risk_eur,
                status=status.value, mode=self.mode.value, purpose="OPEN", reason_code=request.reason_code.value, reason=request.reason,
                risk_checks=decision.model_dump(mode="json"),
            )

    async def _update_order(self, client_order_id: str, *, status: OrderStatus, **values: Any) -> None:
        async with session_scope() as session:
            await Repository(session).update_order(client_order_id, status=status.value, **{k: v for k, v in values.items() if v is not None})

    async def snapshot_portfolio(self) -> dict[str, Any]:
        """Snapshot periodico per dashboard/drawdown (sez. 48)."""
        context = await self.portfolio_context()
        account = context.account
        from risk.correlation import build_exposure

        report = build_exposure(context.open_positions)
        unrealized = self.paper.unrealized_pnl() if self.mode in (ExecutionMode.PAPER, ExecutionMode.SHADOW) else account.profit_loss
        peak = max(context.peak_equity_week, account.equity)
        async with session_scope() as session:
            repo = Repository(session)
            total_realized = await repo.realized_pnl_since(utcnow() - timedelta(days=3650), self.mode.value)
            await repo.add_portfolio_snapshot(
                mode=self.mode.value, balance=account.balance, equity=account.equity, margin_used=account.margin_used,
                free_margin=account.free_margin, open_risk=context.open_risk_eur, open_notional=report.total_notional,
                realized_pnl_day=context.realized_pnl_today, realized_pnl_total=total_realized, unrealized_pnl=unrealized,
                open_positions=len(context.open_positions), daily_drawdown=max(0.0, -context.realized_pnl_today / account.equity) if account.equity else 0.0,
                weekly_drawdown=(peak - account.equity) / peak if peak else 0.0, peak_equity=peak,
                factor_exposure=report.as_dict()["factor_exposure"], source=account.source,
            )
        return {"equity": account.equity, "balance": account.balance, "margin_used": account.margin_used, "open_risk": context.open_risk_eur, "open_positions": len(context.open_positions), "realized_pnl_today": context.realized_pnl_today, "unrealized_pnl": unrealized, "factor_exposure": report.as_dict()["factor_exposure"]}
