"""Ordini IG (patch sez. 3/10/14/24/26): open, close, amend con conferma obbligatoria."""
from __future__ import annotations

import time
from typing import Any

from core.clock import utcnow
from core.enums import Direction, EntryType, OrderStatus
from core.errors import ExecutionError
from core.logging import get_logger
from core.schemas import DealConfirmation, OrderRequest, OrderResult
from execution.ig.confirmations import await_confirmation
from quant.residual_alpha import slippage_pct

log = get_logger("execution.ig.orders")


def build_open_payload(request: OrderRequest, *, currency: str) -> dict[str, Any]:
    """POST /positions/otc v2. Stop obbligatorio via stopDistance/stopLevel."""
    payload: dict[str, Any] = {
        "epic": request.epic,
        "expiry": request.expiry,
        "direction": request.direction.value,
        "size": request.size,
        "orderType": "MARKET" if request.entry_type is EntryType.MARKET else "LIMIT",
        "guaranteedStop": request.guaranteed_stop,
        "forceOpen": request.force_open,
        "currencyCode": currency,
        "dealReference": request.client_order_id[:30],
    }
    if request.entry_type is EntryType.MARKET:
        payload["timeInForce"] = request.time_in_force if request.time_in_force in ("FILL_OR_KILL", "EXECUTE_AND_ELIMINATE") else "FILL_OR_KILL"
    else:
        payload["level"] = request.level if request.level is not None else request.max_entry
        payload["timeInForce"] = "EXECUTE_AND_ELIMINATE"
    if request.stop_level is not None:
        payload["stopLevel"] = request.stop_level
    else:
        payload["stopDistance"] = request.stop_distance
    if request.limit_level is not None:
        payload["limitLevel"] = request.limit_level
    elif request.limit_distance is not None:
        payload["limitDistance"] = request.limit_distance
    return payload


def build_close_payload(*, deal_id: str, direction: Direction, size: float, level: float | None = None) -> dict[str, Any]:
    """DELETE /positions/otc: direzione OPPOSTA alla posizione."""
    payload: dict[str, Any] = {
        "dealId": deal_id,
        "direction": direction.opposite.value,
        "size": size,
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
    }
    if level is not None:
        payload["orderType"] = "LIMIT"
        payload["level"] = level
    return payload


def build_amend_payload(*, stop_level: float | None, limit_level: float | None, trailing: bool = False) -> dict[str, Any]:
    return {"stopLevel": stop_level, "limitLevel": limit_level, "trailingStop": trailing}


class IGOrderGateway:
    """Esegue ordini su IG e restituisce sempre un esito confermato."""

    def __init__(self, client: Any, *, confirm_attempts: int = 8, confirm_interval_s: float = 0.5):
        self.client = client
        self.confirm_attempts = confirm_attempts
        self.confirm_interval_s = confirm_interval_s

    async def open(self, request: OrderRequest, *, currency: str) -> OrderResult:
        payload = build_open_payload(request, currency=currency)
        timings: dict[str, float] = {"order_submission_ts": utcnow().timestamp()}
        started = time.perf_counter()
        deal_reference = await self.client.create_position(payload)
        timings["exchange_ack_ts"] = utcnow().timestamp()
        timings["submit_latency_ms"] = (time.perf_counter() - started) * 1000
        confirmation = await await_confirmation(
            self.client, deal_reference, attempts=self.confirm_attempts, interval_s=self.confirm_interval_s
        )
        timings["confirm_ts"] = utcnow().timestamp()
        return self._result_from_confirmation(request, deal_reference, confirmation, timings)

    async def close(self, *, client_order_id: str, trade_id: str, deal_id: str, epic: str, direction: Direction, size: float, reference_price: float) -> OrderResult:
        payload = build_close_payload(deal_id=deal_id, direction=direction, size=size)
        timings: dict[str, float] = {"order_submission_ts": utcnow().timestamp()}
        deal_reference = await self.client.close_position(payload)
        timings["exchange_ack_ts"] = utcnow().timestamp()
        confirmation = await await_confirmation(self.client, deal_reference, attempts=self.confirm_attempts, interval_s=self.confirm_interval_s)
        timings["confirm_ts"] = utcnow().timestamp()
        fill = confirmation.level
        return OrderResult(
            client_order_id=client_order_id,
            deal_reference=deal_reference,
            deal_id=confirmation.deal_id or deal_id,
            status=OrderStatus.FILLED.value if confirmation.accepted else OrderStatus.REJECTED.value,
            filled_size=confirmation.size or (size if confirmation.accepted else 0.0),
            fill_price=fill,
            requested_size=size,
            slippage_pct=slippage_pct(fill, reference_price, direction.opposite) if fill else None,
            error=None if confirmation.accepted else (confirmation.reason or "REJECTED"),
            confirmation=confirmation,
            timings=timings,
            raw={"profit": confirmation.profit, "profit_currency": confirmation.profit_currency},
        )

    async def amend(self, *, deal_id: str, stop_level: float | None, limit_level: float | None) -> DealConfirmation:
        deal_reference = await self.client.update_position(deal_id, build_amend_payload(stop_level=stop_level, limit_level=limit_level))
        if not deal_reference:
            raise ExecutionError("IG update_position senza dealReference")
        return await await_confirmation(self.client, deal_reference, attempts=self.confirm_attempts, interval_s=self.confirm_interval_s)

    @staticmethod
    def _result_from_confirmation(request: OrderRequest, deal_reference: str, confirmation: DealConfirmation, timings: dict[str, float]) -> OrderResult:
        if not confirmation.accepted:
            return OrderResult(
                client_order_id=request.client_order_id,
                deal_reference=deal_reference,
                deal_id=confirmation.deal_id,
                status=OrderStatus.REJECTED.value,
                requested_size=request.size,
                error=confirmation.reason or "REJECTED",
                confirmation=confirmation,
                timings=timings,
            )
        fill = confirmation.level
        slip = slippage_pct(fill, request.reference_price, request.direction) if fill else None
        return OrderResult(
            client_order_id=request.client_order_id,
            deal_reference=deal_reference,
            deal_id=confirmation.deal_id,
            status=OrderStatus.FILLED.value,
            filled_size=confirmation.size or request.size,
            fill_price=fill,
            requested_size=request.size,
            slippage_pct=slip,
            confirmation=confirmation,
            timings=timings,
        )
