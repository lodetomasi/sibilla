"""Paper broker (sez. 32): simula IG con prezzi live reali (bid/ask), slippage,
commissioni, stop/limit, time stop. Stessa interfaccia del gateway IG.

Iron rule: nessun prezzo inventato -> ogni fill usa una Quote reale (source
esplicita); nessun fill se il mercato non e' TRADEABLE o la quote e' stale.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.clock import utcnow
from core.config import get_settings
from core.enums import Direction, MarketStatus, OrderStatus
from core.errors import ExecutionError, StaleDataError
from core.logging import get_logger
from core.pricing import (
    PriceConvention,
    is_price_acceptable,
    limit_level,
    margin_required,
    notional,
    pnl_money,
    stop_level,
)
from core.schemas import (
    AccountState,
    BrokerPosition,
    DealConfirmation,
    Instrument,
    OrderRequest,
    OrderResult,
    Quote,
)
from quant.residual_alpha import slippage_pct

log = get_logger("execution.paper")


@dataclass
class PaperPosition:
    deal_id: str
    epic: str
    direction: Direction
    size: float
    level: float
    stop_level: float
    limit_level: float | None
    value_per_point: float
    margin: float
    opened_at: datetime
    currency: str
    trade_id: str
    commission_paid: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class PaperBroker:
    """Conto simulato con equity, margine e posizioni in memoria + storico fill."""

    def __init__(self, *, starting_balance: float | None = None, commission_pct: float = 0.0, slippage_pct: float | None = None, account_id: str = "PAPER"):
        settings = get_settings()
        self.balance = starting_balance if starting_balance is not None else settings.risk.bankroll
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct if slippage_pct is not None else min(0.0002, settings.risk.max_slippage_pct)
        self.account_id = account_id
        self.positions: dict[str, PaperPosition] = {}
        self.closed: list[dict[str, Any]] = []
        self.realized_pnl = 0.0
        self._quotes: dict[str, Quote] = {}
        self.max_staleness_s = settings.risk.max_data_staleness_s

    # ------------------------------------------------------------ quote feed
    def update_quote(self, quote: Quote) -> None:
        self._quotes[quote.epic] = quote

    def _fresh_quote(self, epic: str, quote: Quote | None) -> Quote:
        q = quote or self._quotes.get(epic)
        if q is None:
            raise ExecutionError(f"paper: nessuna quote per {epic}")
        if q.age_seconds() > max(self.max_staleness_s, 60.0):
            raise StaleDataError(f"paper: quote {epic} stale ({q.age_seconds():.0f}s)")
        if not q.market_status.tradeable:
            raise ExecutionError(f"paper: mercato {epic} non tradeable ({q.market_status.value})")
        return q

    # ------------------------------------------------------------------ open
    async def open(self, request: OrderRequest, instrument: Instrument, quote: Quote | None = None) -> OrderResult:
        q = self._fresh_quote(request.epic, quote)
        base = q.price_for(request.direction)
        # slippage simulato deterministico (frazione dello spread + slippage config)
        fill = base * (1 + self.slippage_pct) if request.direction is Direction.BUY else base * (1 - self.slippage_pct)
        timings = {"order_submission_ts": utcnow().timestamp(), "exchange_ack_ts": utcnow().timestamp(), "fill_ts": utcnow().timestamp()}
        if not is_price_acceptable(fill, request.max_entry, PriceConvention.CFD, request.direction.value):
            confirmation = DealConfirmation(deal_reference=request.client_order_id, deal_status="REJECTED", reason="SLIPPAGE_GUARD", epic=request.epic, direction=request.direction, size=request.size, level=fill)
            return OrderResult(client_order_id=request.client_order_id, deal_reference=request.client_order_id, status=OrderStatus.REJECTED.value, requested_size=request.size, error="SLIPPAGE_GUARD: fill oltre max_entry", confirmation=confirmation, timings=timings)
        margin = margin_required(fill, request.size, instrument.value_per_point, instrument.margin_factor)
        equity = self.equity()
        if margin + self.margin_used() > equity:
            confirmation = DealConfirmation(deal_reference=request.client_order_id, deal_status="REJECTED", reason="INSUFFICIENT_FUNDS", epic=request.epic)
            return OrderResult(client_order_id=request.client_order_id, deal_reference=request.client_order_id, status=OrderStatus.REJECTED.value, requested_size=request.size, error="INSUFFICIENT_FUNDS", confirmation=confirmation, timings=timings)
        deal_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
        stop = request.stop_level if request.stop_level is not None else stop_level(fill, request.stop_distance, request.direction.value)
        limit = request.limit_level if request.limit_level is not None else (limit_level(fill, request.limit_distance, request.direction.value) if request.limit_distance else None)
        commission = notional(fill, request.size, instrument.value_per_point) * self.commission_pct
        self.balance -= commission
        self.positions[deal_id] = PaperPosition(
            deal_id=deal_id, epic=request.epic, direction=request.direction, size=request.size, level=fill,
            stop_level=stop, limit_level=limit, value_per_point=instrument.value_per_point, margin=margin,
            opened_at=utcnow(), currency=instrument.currency, trade_id=request.trade_id, commission_paid=commission,
            raw={"quote_source": q.source},
        )
        confirmation = DealConfirmation(
            deal_reference=request.client_order_id, deal_id=deal_id, deal_status="ACCEPTED", status="OPEN",
            epic=request.epic, direction=request.direction, size=request.size, level=fill, stop_level=stop, limit_level=limit,
            date=utcnow(), raw={"paper": True, "quote_source": q.source},
        )
        log.info("paper.open", deal_id=deal_id, epic=request.epic, direction=request.direction.value, size=request.size, fill=fill, stop=stop, limit=limit, source=q.source)
        return OrderResult(
            client_order_id=request.client_order_id, deal_reference=request.client_order_id, deal_id=deal_id,
            status=OrderStatus.FILLED.value, filled_size=request.size, fill_price=fill, requested_size=request.size,
            slippage_pct=slippage_pct(fill, request.reference_price, request.direction), commission=commission,
            confirmation=confirmation, timings=timings, raw={"quote_source": q.source},
        )

    # ----------------------------------------------------------------- close
    async def close(self, deal_id: str, *, quote: Quote | None = None, reason: str = "MANUAL", level_override: float | None = None) -> OrderResult:
        position = self.positions.get(deal_id)
        if position is None:
            raise ExecutionError(f"paper: posizione {deal_id} inesistente")
        q = self._fresh_quote(position.epic, quote) if level_override is None else None
        fill = level_override if level_override is not None else q.exit_price_for(position.direction)  # type: ignore[union-attr]
        if level_override is None:
            fill = fill * (1 - self.slippage_pct) if position.direction is Direction.BUY else fill * (1 + self.slippage_pct)
        pnl = pnl_money(position.level, fill, position.direction.value, position.size, position.value_per_point)
        commission = notional(fill, position.size, position.value_per_point) * self.commission_pct
        self.balance += pnl - commission
        self.realized_pnl += pnl - commission
        del self.positions[deal_id]
        record = {
            "deal_id": deal_id, "trade_id": position.trade_id, "epic": position.epic, "direction": position.direction.value,
            "size": position.size, "entry": position.level, "exit": fill, "pnl": pnl - commission, "reason": reason,
            "opened_at": position.opened_at, "closed_at": utcnow(), "commission": commission + position.commission_paid,
        }
        self.closed.append(record)
        timings = {"order_submission_ts": utcnow().timestamp(), "fill_ts": utcnow().timestamp()}
        confirmation = DealConfirmation(deal_reference=f"CLOSE-{deal_id}", deal_id=deal_id, deal_status="ACCEPTED", status="CLOSED", epic=position.epic, direction=position.direction.opposite, size=position.size, level=fill, profit=pnl - commission, profit_currency=position.currency, date=utcnow(), raw={"paper": True, "reason": reason})
        log.info("paper.close", deal_id=deal_id, epic=position.epic, exit=fill, pnl=round(pnl - commission, 2), reason=reason)
        return OrderResult(client_order_id=f"CLOSE-{deal_id}", deal_reference=f"CLOSE-{deal_id}", deal_id=deal_id, status=OrderStatus.FILLED.value, filled_size=position.size, fill_price=fill, requested_size=position.size, commission=commission, confirmation=confirmation, timings=timings, raw={"pnl": pnl - commission, "reason": reason})

    async def amend(self, deal_id: str, *, stop_level: float | None = None, limit_level: float | None = None) -> DealConfirmation:
        position = self.positions.get(deal_id)
        if position is None:
            raise ExecutionError(f"paper: posizione {deal_id} inesistente")
        if stop_level is not None:
            position.stop_level = stop_level
        if limit_level is not None:
            position.limit_level = limit_level
        return DealConfirmation(deal_reference=f"AMEND-{deal_id}", deal_id=deal_id, deal_status="ACCEPTED", status="AMENDED", epic=position.epic, stop_level=position.stop_level, limit_level=position.limit_level, date=utcnow())

    # -------------------------------------------------------------- mark/stop
    async def mark_to_market(self, quotes: dict[str, Quote]) -> list[OrderResult]:
        """Aggiorna quote e chiude le posizioni che toccano stop/limit (worst-case: stop prima del limit)."""
        results: list[OrderResult] = []
        for quote in quotes.values():
            self.update_quote(quote)
        for deal_id, position in list(self.positions.items()):
            quote = self._quotes.get(position.epic)
            if quote is None or not quote.market_status.tradeable:
                continue
            exit_px = quote.exit_price_for(position.direction)
            stop_hit = exit_px <= position.stop_level if position.direction is Direction.BUY else exit_px >= position.stop_level
            limit_hit = position.limit_level is not None and (exit_px >= position.limit_level if position.direction is Direction.BUY else exit_px <= position.limit_level)
            if stop_hit:
                results.append(await self.close(deal_id, reason="STOP_HIT", level_override=position.stop_level))
            elif limit_hit:
                results.append(await self.close(deal_id, reason="TARGET_HIT", level_override=position.limit_level))
        return results

    # ------------------------------------------------------------------ conto
    def unrealized_pnl(self) -> float:
        total = 0.0
        for position in self.positions.values():
            quote = self._quotes.get(position.epic)
            if quote is None:
                continue
            total += pnl_money(position.level, quote.exit_price_for(position.direction), position.direction.value, position.size, position.value_per_point)
        return total

    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def equity(self) -> float:
        return self.balance + self.unrealized_pnl()

    def account_state(self) -> AccountState:
        return AccountState(
            account_id=self.account_id, currency=get_settings().base_currency, balance=self.balance, deposit=self.margin_used(),
            profit_loss=self.unrealized_pnl(), available=self.equity() - self.margin_used(), margin_used=self.margin_used(),
            equity=self.equity(), ts=utcnow(), source="paper",
        )

    def load_open_positions(self, rows: list[Any]) -> int:
        """Reidrata le posizioni PAPER aperte dal DB (sopravvivenza ai riavvii del runner).

        Senza questo, una posizione PAPER aperta in un run precedente resterebbe nel DB
        ma il monitor non potrebbe gestirne stop/target perche' il broker in-memory e' vuoto.
        """
        loaded = 0
        for row in rows:
            deal_id = row.deal_id or f"PAPER-{row.trade_id}"
            if deal_id in self.positions:
                continue
            self.positions[deal_id] = PaperPosition(
                deal_id=deal_id, epic=row.epic, direction=Direction.parse(row.direction), size=float(row.size),
                level=float(row.entry_price), stop_level=float(row.stop_level) if row.stop_level is not None else float(row.entry_price),
                limit_level=float(row.limit_level) if row.limit_level is not None else None,
                value_per_point=float(row.value_per_point or 1.0), margin=float(row.margin_required or 0.0),
                opened_at=row.opened_at, currency=row.currency or "EUR", trade_id=row.trade_id,
                commission_paid=float(row.commission_paid or 0.0), raw={"rehydrated": True},
            )
            loaded += 1
        return loaded

    def broker_positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for position in self.positions.values():
            quote = self._quotes.get(position.epic)
            out.append(BrokerPosition(
                deal_id=position.deal_id, epic=position.epic, direction=position.direction, size=position.size, level=position.level,
                stop_level=position.stop_level, limit_level=position.limit_level, currency=position.currency, created_at=position.opened_at,
                deal_reference=position.trade_id, bid=quote.bid if quote else None, offer=quote.offer if quote else None,
                market_status=quote.market_status if quote else MarketStatus.UNKNOWN,
            ))
        return out
