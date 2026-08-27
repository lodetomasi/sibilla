"""Posizioni e conto IG -> schemi interni (patch sez. 3/24/28)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.clock import utcnow
from core.enums import Direction, MarketStatus
from core.schemas import AccountState, BrokerPosition


def parse_broker_position(raw: dict[str, Any]) -> BrokerPosition:
    position = raw.get("position") or raw
    market = raw.get("market") or {}
    return BrokerPosition(
        deal_id=str(position.get("dealId")),
        epic=str(market.get("epic") or position.get("epic") or ""),
        direction=Direction.parse(str(position.get("direction") or "BUY")),
        size=float(position.get("size") or position.get("dealSize") or 0.0),
        level=float(position.get("level") or position.get("openLevel") or 0.0),
        stop_level=_f(position.get("stopLevel")),
        limit_level=_f(position.get("limitLevel")),
        currency=str(position.get("currency") or "EUR"),
        created_at=_parse(position.get("createdDateUTC") or position.get("createdDate")),
        deal_reference=position.get("dealReference"),
        bid=_f(market.get("bid")),
        offer=_f(market.get("offer")),
        market_status=MarketStatus.parse(market.get("marketStatus")),
        contract_size=_f(position.get("contractSize")),
        controlled_risk=bool(position.get("controlledRisk", False)),
        raw={"instrument_name": market.get("instrumentName"), "expiry": market.get("expiry"), "trailing_step": position.get("trailingStep")},
    )


def parse_account_state(raw: dict[str, Any], *, account_id: str) -> AccountState:
    balance = raw.get("balance") or {}
    bal = float(balance.get("balance") or 0.0)
    deposit = float(balance.get("deposit") or 0.0)  # margine impegnato
    pnl = float(balance.get("profitLoss") or 0.0)
    available = float(balance.get("available") or 0.0)
    return AccountState(
        account_id=str(raw.get("accountId") or account_id),
        currency=str(raw.get("currency") or "EUR"),
        balance=bal,
        deposit=deposit,
        profit_loss=pnl,
        available=available,
        margin_used=deposit,
        equity=bal + pnl,
        ts=utcnow(),
        source="ig-rest",
    )


def account_state_from_stream(values: dict[str, Any], *, account_id: str, currency: str = "EUR") -> AccountState | None:
    equity = values.get("EQUITY")
    funds = values.get("FUNDS")
    if equity is None and funds is None:
        return None
    balance = float(funds if funds is not None else equity)
    pnl = float(values.get("PNL") or 0.0)
    margin = float(values.get("MARGIN") or values.get("DEPOSIT") or 0.0)
    return AccountState(
        account_id=account_id,
        currency=currency,
        balance=balance,
        deposit=margin,
        profit_loss=pnl,
        available=float(values.get("AVAILABLE_TO_DEAL") or values.get("AVAILABLE_CASH") or 0.0),
        margin_used=margin,
        equity=float(equity) if equity is not None else balance + pnl,
        ts=utcnow(),
        source="ig-stream",
    )


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
