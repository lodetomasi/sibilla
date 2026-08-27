"""Base comune ai collector: modalita batch/incremental/live (sez. 4.1)."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.clock import utcnow
from core.logging import get_logger


class CollectionMode(str, Enum):
    HISTORICAL_BATCH = "historical_batch"
    INCREMENTAL = "incremental"
    LIVE = "live"


@dataclass
class CollectorStats:
    name: str
    runs: int = 0
    items: int = 0
    errors: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_duration_s: float = 0.0
    watermark: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "runs": self.runs,
            "items": self.items,
            "errors": self.errors,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "last_duration_s": round(self.last_duration_s, 3),
            "watermark": self.watermark.isoformat() if self.watermark else None,
            **self.details,
        }


class BaseCollector(ABC):
    """Contratto dei collector.

    `collect` esegue un ciclo e restituisce il numero di elementi raccolti;
    `run_forever` lo ripete a intervallo con backoff sugli errori.
    """

    name = "collector"

    def __init__(self) -> None:
        self.log = get_logger(f"collectors.{self.name}")
        self.stats = CollectorStats(name=self.name)

    @abstractmethod
    async def collect(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        ...

    async def run_once(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        started = asyncio.get_event_loop().time()
        self.stats.runs += 1
        try:
            count = await self.collect(mode, **kwargs)
            self.stats.items += count
            self.stats.last_run_at = utcnow()
            self.stats.last_duration_s = asyncio.get_event_loop().time() - started
            self.log.info("collector.run", mode=mode.value, items=count,
                          duration_s=round(self.stats.last_duration_s, 2))
            return count
        except Exception as exc:  # noqa: BLE001 - il worker non deve morire
            self.stats.errors += 1
            self.stats.last_error = str(exc)[:300]
            self.log.error("collector.error", mode=mode.value, error=str(exc)[:300])
            raise

    async def run_forever(
        self,
        interval_s: float = 60.0,
        mode: CollectionMode = CollectionMode.INCREMENTAL,
        **kwargs: Any,
    ) -> None:
        backoff = interval_s
        while True:
            try:
                await self.run_once(mode, **kwargs)
                backoff = interval_s
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                backoff = min(backoff * 2, 600.0)
            await asyncio.sleep(backoff)

    async def aclose(self) -> None:
        return None
