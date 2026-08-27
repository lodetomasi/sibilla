"""Feed live CLOB via WebSocket (sez. 4.1: live/WebSocket quando disponibile).

Canale `market`: book snapshot, price_change, last_trade_price. Il feed aggiorna
la cache dei prezzi correnti (sez. 44) ed emette eventi sul bus (sez. 47).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import orjson
import websockets

from core.bus import emit
from core.cache import get_cache
from core.clock import utcnow
from core.config import get_settings
from core.enums import EventType
from core.logging import get_logger
from core.pricing import PriceConvention
from core.schemas import BookLevel, OrderBook

log = get_logger("collectors.polymarket.ws")


class PolymarketWebSocket:
    """Client WS con riconnessione automatica e backoff."""

    def __init__(
        self,
        asset_ids: list[str] | None = None,
        *,
        url: str | None = None,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
    ):
        settings = get_settings()
        self.url = (url or settings.polymarket.ws_url).rstrip("/") + "/market"
        self.asset_ids: list[str] = list(asset_ids or [])
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        self.books: dict[str, OrderBook] = {}
        self.last_message_at = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.messages_received = 0
        self.reconnects = 0
        self._bypass_ip: str | None = None

    def set_assets(self, asset_ids: list[str]) -> None:
        self.asset_ids = list(dict.fromkeys(asset_ids))

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def healthy(self) -> bool:
        """Il feed e' sano se ha ricevuto un messaggio negli ultimi 60s."""
        if self.last_message_at is None:
            return False
        return (utcnow() - self.last_message_at).total_seconds() < 60

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                connect_kwargs: dict[str, Any] = {"ping_interval": self.ping_interval, "close_timeout": 5}
                if self._bypass_ip:
                    import ssl
                    from urllib.parse import urlsplit

                    host = urlsplit(self.url).hostname or ""
                    connect_kwargs.update({"host": self._bypass_ip, "port": 443, "ssl": ssl.create_default_context(), "server_hostname": host, "additional_headers": {"Host": host}})
                async with websockets.connect(self.url, **connect_kwargs) as socket:
                    await socket.send(
                        orjson.dumps({"assets_ids": self.asset_ids, "type": "market"}).decode()
                    )
                    log.info("ws.connected", assets=len(self.asset_ids))
                    backoff = 1.0
                    async for raw in socket:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - il feed deve sempre riprovare
                self.reconnects += 1
                if not self._bypass_ip and ("CERTIFICATE_VERIFY_FAILED" in str(exc) or "Hostname mismatch" in str(exc)):
                    from urllib.parse import urlsplit

                    from core.http import resolve_doh

                    ips = await resolve_doh(urlsplit(self.url).hostname or "")
                    if ips:
                        self._bypass_ip = ips[0]
                        log.warning("ws.dns_bypass.enabled", ip=self._bypass_ip)
                        continue
                log.warning("ws.disconnected", error=str(exc)[:160], backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def _handle_raw(self, raw: str | bytes) -> None:
        self.messages_received += 1
        self.last_message_at = utcnow()
        try:
            payload = orjson.loads(raw)
        except Exception:  # noqa: BLE001
            return
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if isinstance(message, dict):
                await self.handle_message(message)

    async def handle_message(self, message: dict[str, Any]) -> None:
        event_type = message.get("event_type") or message.get("type")
        asset_id = str(message.get("asset_id") or message.get("market") or "")
        if event_type == "book":
            book = self._book_from_message(message, asset_id)
            self.books[asset_id] = book
            await self._publish_price(asset_id, book.mid, book)
        elif event_type == "price_change":
            book = self._apply_price_changes(message, asset_id)
            if book is not None:
                await self._publish_price(asset_id, book.mid, book)
        elif event_type == "last_trade_price":
            price = _f(message.get("price"))
            if price is not None:
                await self._publish_price(asset_id, price, self.books.get(asset_id))
        elif event_type == "tick_size_change":
            log.info("ws.tick_size_change", asset=asset_id, payload=message)

    def _book_from_message(self, message: dict[str, Any], asset_id: str) -> OrderBook:
        bids = [
            BookLevel(price=float(level["price"]), size=float(level["size"]))
            for level in message.get("bids") or message.get("buys") or []
            if _f(level.get("price")) is not None
        ]
        asks = [
            BookLevel(price=float(level["price"]), size=float(level["size"]))
            for level in message.get("asks") or message.get("sells") or []
            if _f(level.get("price")) is not None
        ]
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return OrderBook(
            venue="polymarket",
            market_id=str(message.get("market") or asset_id),
            outcome=str(message.get("outcome") or "YES"),
            ts=utcnow(),
            bids=bids,
            asks=asks,
            price_convention=PriceConvention.PROBABILITY,
        )

    def _apply_price_changes(self, message: dict[str, Any], asset_id: str) -> OrderBook | None:
        book = self.books.get(asset_id)
        if book is None:
            book = OrderBook(
                venue="polymarket",
                market_id=asset_id,
                price_convention=PriceConvention.PROBABILITY,
            )
        changes = message.get("changes") or message.get("price_changes") or []
        if isinstance(changes, dict):
            changes = [changes]
        levels = {"BUY": {lvl.price: lvl.size for lvl in book.bids},
                  "SELL": {lvl.price: lvl.size for lvl in book.asks}}
        for change in changes:
            price = _f(change.get("price"))
            size = _f(change.get("size"), 0.0)
            side = str(change.get("side", "BUY")).upper()
            if price is None:
                continue
            bucket = levels.get("BUY" if side in ("BUY", "BID") else "SELL")
            if bucket is None:
                continue
            if size and size > 0:
                bucket[price] = size
            else:
                bucket.pop(price, None)
        book.bids = [
            BookLevel(price=price, size=size)
            for price, size in sorted(levels["BUY"].items(), reverse=True)
        ]
        book.asks = [
            BookLevel(price=price, size=size) for price, size in sorted(levels["SELL"].items())
        ]
        book.ts = utcnow()
        self.books[asset_id] = book
        return book

    async def _publish_price(
        self, asset_id: str, price: float | None, book: OrderBook | None
    ) -> None:
        if price is None:
            return
        cache = await get_cache(get_settings().redis_url)
        await cache.set_json(
            f"price:polymarket:{asset_id}",
            {
                "price": price,
                "best_bid": book.best_bid if book else None,
                "best_ask": book.best_ask if book else None,
                "ts": utcnow().isoformat(),
            },
            ttl_s=120,
        )
        await emit(
            EventType.PRICE_CHANGED,
            {
                "venue": "polymarket",
                "asset_id": asset_id,
                "market_id": book.market_id if book else asset_id,
                "price": price,
                "best_bid": book.best_bid if book else None,
                "best_ask": book.best_ask if book else None,
                "source": "ws",
            },
            source="polymarket_ws",
        )


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
