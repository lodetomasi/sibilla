"""Streaming IG via Lightstreamer (patch sez. 25).

Sottoscrizioni:
  MARKET:{epic}      MERGE   BID, OFFER, MARKET_STATE, UPDATE_TIME, ...
  ACCOUNT:{accId}    MERGE   PNL, DEPOSIT, AVAILABLE_CASH, FUNDS, MARGIN, EQUITY, ...
  TRADE:{accId}      DISTINCT CONFIRMS, OPU, WOU
Gli aggiornamenti alimentano PriceService e il bus.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.bus import emit
from core.clock import utcnow
from core.enums import EventType, MarketStatus
from core.logging import get_logger
from core.schemas import Quote
from execution.ig.auth import IGSession
from market.prices import PriceService

log = get_logger("market.streaming")

MARKET_FIELDS = ["BID", "OFFER", "HIGH", "LOW", "CHANGE", "CHANGE_PCT", "MARKET_DELAY", "MARKET_STATE", "UPDATE_TIME"]
ACCOUNT_FIELDS = ["PNL", "DEPOSIT", "AVAILABLE_CASH", "FUNDS", "MARGIN", "AVAILABLE_TO_DEAL", "EQUITY", "EQUITY_USED"]
TRADE_FIELDS = ["CONFIRMS", "OPU", "WOU"]


class IGStreamingClient:
    """Wrapper asincrono attorno al client Lightstreamer ufficiale."""

    def __init__(
        self,
        session: IGSession,
        price_service: PriceService,
        *,
        on_account: Callable[[dict[str, Any]], Any] | None = None,
        on_trade: Callable[[str, dict[str, Any]], Any] | None = None,
    ):
        self.session = session
        self.prices = price_service
        self.on_account = on_account
        self.on_trade = on_trade
        self._client: Any = None
        self._subscriptions: dict[str, Any] = {}
        self._loop = asyncio.get_event_loop()
        self.connected = False
        self.last_update_at: datetime | None = None
        self._last_market_update_at: datetime | None = None
        self.updates = 0
        self.account_state: dict[str, Any] = {}

    async def start(self, epics: list[str]) -> None:
        from lightstreamer.client import LightstreamerClient, Subscription

        client = LightstreamerClient(self.session.lightstreamer_endpoint, None)
        client.connectionDetails.setUser(self.session.account_id)
        client.connectionDetails.setPassword(self.session.streaming_password())
        client.addListener(_ClientListener(self))
        self._client = client
        client.connect()

        market_items = [f"MARKET:{epic}" for epic in epics]
        if market_items:
            sub = Subscription("MERGE", market_items, MARKET_FIELDS)
            sub.addListener(_MarketListener(self))
            client.subscribe(sub)
            self._subscriptions["market"] = sub

        account_sub = Subscription("MERGE", [f"ACCOUNT:{self.session.account_id}"], ACCOUNT_FIELDS)
        account_sub.addListener(_AccountListener(self))
        client.subscribe(account_sub)
        self._subscriptions["account"] = account_sub

        trade_sub = Subscription("DISTINCT", [f"TRADE:{self.session.account_id}"], TRADE_FIELDS)
        trade_sub.addListener(_TradeListener(self))
        client.subscribe(trade_sub)
        self._subscriptions["trade"] = trade_sub
        log.info("ig.stream.started", epics=len(epics))

    async def subscribe_prices(self, epics: list[str]) -> None:
        """Aggiunge epic al feed (ricrea la subscription MARKET)."""
        if self._client is None:
            return
        from lightstreamer.client import Subscription

        old = self._subscriptions.get("market")
        if old is not None:
            with contextlib.suppress(Exception):
                self._client.unsubscribe(old)
        sub = Subscription("MERGE", [f"MARKET:{e}" for e in epics], MARKET_FIELDS)
        sub.addListener(_MarketListener(self))
        self._client.subscribe(sub)
        self._subscriptions["market"] = sub

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.disconnect()
        self.connected = False

    @property
    def healthy(self) -> bool:
        # "sano" solo se arrivano tick di PREZZO (MARKET), non i semplici heartbeat di conto/trade:
        # altrimenti il price collector strozzerebbe il REST pensando che lo stream copra i prezzi.
        if not self.connected or self._last_market_update_at is None:
            return False
        return (utcnow() - self._last_market_update_at).total_seconds() < 120

    # ------------------------------------------------------- thread -> loop
    def _dispatch(self, coro: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            pass

    def handle_market_update(self, item: str, values: dict[str, Any]) -> None:
        """Chiamato dal thread Lightstreamer."""
        epic = item.split(":", 1)[1] if ":" in item else item
        bid, offer = _f(values.get("BID")), _f(values.get("OFFER"))
        if bid is None or offer is None:
            return
        self.updates += 1
        self.last_update_at = utcnow()
        self._last_market_update_at = utcnow()
        quote = Quote(
            epic=epic,
            bid=bid,
            offer=offer,
            ts=_parse_update_time(values.get("UPDATE_TIME")),
            market_status=MarketStatus.parse(values.get("MARKET_STATE")),
            source="ig-stream",
            high=_f(values.get("HIGH")),
            low=_f(values.get("LOW")),
            change_pct=(_f(values.get("CHANGE_PCT")) or 0.0) / 100.0,
            delay_ms=int(_f(values.get("MARKET_DELAY")) or 0) * 1000,
        )
        self.prices.push_live(quote)
        self._dispatch(
            emit(
                EventType.PRICE_CHANGED,
                {"venue": "ig", "epic": epic, "bid": bid, "offer": offer,
                 "market_status": quote.market_status.value, "source": "ig-stream"},
                source="ig_stream",
            )
        )

    def handle_account_update(self, values: dict[str, Any]) -> None:
        self.last_update_at = utcnow()
        self.account_state = {k: _f(v) for k, v in values.items() if v is not None}
        if self.on_account:
            result = self.on_account(self.account_state)
            if asyncio.iscoroutine(result):
                self._dispatch(result)

    def handle_trade_update(self, values: dict[str, Any]) -> None:
        self.last_update_at = utcnow()
        for key in TRADE_FIELDS:
            raw = values.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {"raw": raw}
            if self.on_trade:
                result = self.on_trade(key, payload)
                if asyncio.iscoroutine(result):
                    self._dispatch(result)
            event = {
                "CONFIRMS": EventType.ORDER_CONFIRMED,
                "OPU": EventType.POSITION_UPDATED,
                "WOU": EventType.ORDER_SUBMITTED,
            }[key]
            self._dispatch(emit(event, {"stream": key, "payload": payload}, source="ig_stream"))


class _ClientListener:
    def __init__(self, owner: IGStreamingClient):
        self.owner = owner

    def onStatusChange(self, status: str) -> None:  # noqa: N802 - API Lightstreamer
        self.owner.connected = status.startswith("CONNECTED")
        log.info("ig.stream.status", status=status)

    def onServerError(self, code: int, message: str) -> None:  # noqa: N802
        log.error("ig.stream.server_error", code=code, message=message)

    def onListenEnd(self, *args: Any) -> None:  # noqa: N802
        return None

    def onListenStart(self, *args: Any) -> None:  # noqa: N802
        return None

    def onPropertyChange(self, *args: Any) -> None:  # noqa: N802
        return None


class _BaseSubListener:
    def __init__(self, owner: IGStreamingClient):
        self.owner = owner

    def onSubscription(self) -> None:  # noqa: N802
        return None

    def onUnsubscription(self) -> None:  # noqa: N802
        return None

    def onSubscriptionError(self, code: int, message: str) -> None:  # noqa: N802
        log.error("ig.stream.subscription_error", code=code, message=message)

    def onListenStart(self, *args: Any) -> None:  # noqa: N802
        return None

    def onListenEnd(self, *args: Any) -> None:  # noqa: N802
        return None

    def onItemLostUpdates(self, *args: Any) -> None:  # noqa: N802
        return None

    def onClearSnapshot(self, *args: Any) -> None:  # noqa: N802
        return None

    def onCommandSecondLevelItemLostUpdates(self, *args: Any) -> None:  # noqa: N802
        return None

    def onCommandSecondLevelSubscriptionError(self, *args: Any) -> None:  # noqa: N802
        return None

    def onEndOfSnapshot(self, *args: Any) -> None:  # noqa: N802
        return None

    def onRealMaxFrequency(self, *args: Any) -> None:  # noqa: N802
        return None

    @staticmethod
    def _values(update: Any, fields: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in fields:
            try:
                out[field] = update.getValue(field)
            except Exception:  # noqa: BLE001
                out[field] = None
        return out


class _MarketListener(_BaseSubListener):
    def onItemUpdate(self, update: Any) -> None:  # noqa: N802
        self.owner.handle_market_update(update.getItemName(), self._values(update, MARKET_FIELDS))


class _AccountListener(_BaseSubListener):
    def onItemUpdate(self, update: Any) -> None:  # noqa: N802
        self.owner.handle_account_update(self._values(update, ACCOUNT_FIELDS))


class _TradeListener(_BaseSubListener):
    def onItemUpdate(self, update: Any) -> None:  # noqa: N802
        self.owner.handle_trade_update(self._values(update, TRADE_FIELDS))


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_update_time(value: Any) -> datetime:
    """UPDATE_TIME arriva come HH:MM:SS (ora UK) - lo ancoriamo a oggi UTC."""
    now = utcnow()
    if not value:
        return now
    try:
        parsed = datetime.strptime(str(value), "%H:%M:%S")
        candidate = now.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)
        if candidate > now:
            candidate = candidate.replace(day=candidate.day) - (candidate - now) * 0  # noqa: B018
        return candidate.replace(tzinfo=UTC)
    except ValueError:
        return now
