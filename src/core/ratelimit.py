"""Rate limiting e circuit breaker per le API esterne."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from core.clock import utcnow
from core.errors import UpstreamError


class TokenBucket:
    """Token bucket asincrono: `rps` richieste/sec con burst configurabile."""

    def __init__(self, rps: float, burst: int | None = None):
        if rps <= 0:
            raise ValueError("rps deve essere > 0")
        self.rps = rps
        self.capacity = float(burst if burst is not None else max(1.0, rps))
        self._tokens = self.capacity
        self._updated = asyncio.get_event_loop().time() if _loop_running() else 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = asyncio.get_event_loop().time()
                elapsed = max(0.0, now - self._updated)
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rps)


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@dataclass
class CircuitBreaker:
    """Apre il circuito dopo N fallimenti consecutivi.

    Serve al kill switch (sez. 27, API_UNAVAILABLE): quando un provider e giu,
    il sistema deve smettere di decidere su dati che non arrivano.
    """

    name: str
    failure_threshold: int = 5
    reset_timeout_s: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if (utcnow().timestamp() - self._opened_at) >= self.reset_timeout_s:
            # half-open: si riprova
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = utcnow().timestamp()

    def guard(self) -> None:
        if self.is_open:
            raise UpstreamError(
                f"circuit breaker aperto per {self.name}", provider=self.name
            )

    @property
    def failures(self) -> int:
        return self._failures
