"""Event bus (sez. 47): Redis Streams in produzione, in-memory come fallback."""
from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import orjson
from pydantic import BaseModel, Field

from core.clock import utcnow
from core.enums import EventType
from core.logging import get_logger

log = get_logger("core.bus")

Handler = Callable[["BusEvent"], Awaitable[None]]


class BusEvent(BaseModel):
    type: EventType
    ts: Any = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "system"

    def encode(self) -> bytes:
        return orjson.dumps(self.model_dump(mode="json"))

    @classmethod
    def decode(cls, raw: bytes | str) -> BusEvent:
        return cls.model_validate(orjson.loads(raw))


class EventBus(Protocol):
    async def publish(self, event: BusEvent) -> None: ...
    def subscribe(self, event_type: EventType, handler: Handler) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class InMemoryBus:
    """Bus di processo con consegna ASINCRONA (coda + consumer).

    `publish` non attende i subscriber: un handler che scrive su DB non puo'
    mai bloccarsi sul lock tenuto dal publisher (che magari e' dentro una
    transazione aperta). Stesso contratto di Redis Streams.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self.history: list[BusEvent] = []
        self.max_history = 2000
        self._queue: asyncio.Queue[BusEvent] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self.delivered = 0
        self.errors = 0

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def _ensure_consumer(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume())

    async def publish(self, event: BusEvent) -> None:
        self.history.append(event)
        if len(self.history) > self.max_history:
            del self.history[: -self.max_history]
        if not self._handlers.get(event.type):
            return
        self._ensure_consumer()
        assert self._queue is not None
        self._queue.put_nowait(event)

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                results = await asyncio.gather(
                    *(handler(event) for handler in list(self._handlers.get(event.type, ()))),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        self.errors += 1
                        log.error("bus.handler.error", event_type=event.type.value, error=str(result)[:300])
                    else:
                        self.delivered += 1
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Attende la consegna di tutti gli eventi in coda (test/shutdown)."""
        if self._queue is not None:
            await self._queue.join()

    def events_of(self, event_type: EventType) -> list[BusEvent]:
        return [e for e in self.history if e.type == event_type]

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue else 0

    async def start(self) -> None:
        self._ensure_consumer()

    async def stop(self) -> None:
        await self.drain()
        if self._consumer:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None


class RedisStreamBus:
    """Bus distribuito su Redis Streams, con consumer group per worker."""

    def __init__(self, url: str, *, stream: str = "ats:events", group: str = "ats"):
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=False)
        self.stream = stream
        self.group = group
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.consumer_name = f"c-{id(self)}"

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: BusEvent) -> None:
        await self._redis.xadd(self.stream, {b"data": event.encode()}, maxlen=100_000)

    async def start(self) -> None:
        try:
            await self._redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - gruppo gia esistente
            if "BUSYGROUP" not in str(exc):
                raise
        self._running = True
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    self.group, self.consumer_name, {self.stream: ">"}, count=32, block=1000
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("bus.redis.read_error", error=str(exc)[:160])
                await asyncio.sleep(1.0)
                continue
            for _stream, entries in messages or []:
                for message_id, fields in entries:
                    raw = fields.get(b"data")
                    if raw:
                        try:
                            event = BusEvent.decode(raw)
                            for handler in self._handlers.get(event.type, ()):
                                await handler(event)
                        except Exception as exc:  # noqa: BLE001
                            log.error("bus.handler.error", error=str(exc)[:200])
                    await self._redis.xack(self.stream, self.group, message_id)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._redis.aclose()


_bus: EventBus | None = None


async def get_bus(redis_url: str | None = None) -> EventBus:
    global _bus
    if _bus is not None:
        return _bus
    if redis_url:
        try:
            bus = RedisStreamBus(redis_url)
            await bus.start()
            _bus = bus
            log.info("bus.redis.started")
            return _bus
        except Exception as exc:  # noqa: BLE001
            log.warning("bus.redis.unavailable", error=str(exc)[:160])
    _bus = InMemoryBus()
    log.info("bus.memory.started")
    return _bus


def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def reset_bus() -> None:
    global _bus
    _bus = None


async def emit(event_type: EventType, payload: dict[str, Any], *, source: str = "system") -> None:
    bus = await get_bus()
    await bus.publish(BusEvent(type=event_type, payload=payload, source=source))
