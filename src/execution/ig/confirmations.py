"""Conferme IG (patch sez. 24): submit -> dealReference -> confirmation -> accepted/rejected."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from core.enums import Direction
from core.errors import UpstreamError
from core.logging import get_logger
from core.schemas import DealConfirmation

log = get_logger("execution.ig.confirmations")


def parse_confirmation(raw: dict[str, Any]) -> DealConfirmation:
    direction = raw.get("direction")
    return DealConfirmation(
        deal_reference=str(raw.get("dealReference") or ""),
        deal_id=raw.get("dealId"),
        deal_status=str(raw.get("dealStatus") or ("ACCEPTED" if raw.get("dealId") else "REJECTED")),
        status=raw.get("status"),
        reason=raw.get("reason"),
        epic=raw.get("epic"),
        direction=Direction.parse(direction) if direction else None,
        size=_f(raw.get("size")),
        level=_f(raw.get("level")),
        stop_level=_f(raw.get("stopLevel")),
        limit_level=_f(raw.get("limitLevel")),
        profit=_f(raw.get("profit")),
        profit_currency=raw.get("profitCurrency"),
        affected_deals=list(raw.get("affectedDeals") or []),
        date=_parse_date(raw.get("date")),
        raw=raw,
    )


async def await_confirmation(client: Any, deal_reference: str, *, attempts: int = 8, interval_s: float = 0.5) -> DealConfirmation:
    """Polling di GET /confirms/{dealReference}: IG puo rispondere 404 per qualche centinaio di ms."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = await client.get_confirm(deal_reference)
            confirmation = parse_confirmation(raw)
            log.info(
                "ig.confirm",
                deal_reference=deal_reference,
                deal_status=confirmation.deal_status,
                status=confirmation.status,
                reason=confirmation.reason,
                attempt=attempt,
            )
            return confirmation
        except UpstreamError as exc:
            last_error = exc
            if exc.status_code not in (404, 400, None):
                raise
            await asyncio.sleep(interval_s * attempt)
    raise UpstreamError(f"conferma non disponibile per {deal_reference}: {last_error}", provider="ig")


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
