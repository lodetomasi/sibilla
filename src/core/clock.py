"""Orologio iniettabile.

Sez. 55 (no lookahead bias): ogni componente deve leggere il tempo da un Clock,
mai da datetime.now() diretto, cosi backtest e replay possono congelare il tempo.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Orologio reale (sempre UTC, timezone-aware)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Orologio manuale per test/backtest: il tempo avanza solo se lo si sposta."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, **kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=UTC)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    """Usato da backtester e test. Mai in produzione a runtime."""
    global _clock
    _clock = clock


def utcnow() -> datetime:
    return _clock.now()


def ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def age_seconds(ts: datetime, *, now: datetime | None = None) -> float:
    return ((now or utcnow()) - ensure_utc(ts)).total_seconds()
