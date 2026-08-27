"""Anomaly detection (sez. 10).

Regola dei requisiti:
    price_move > 3 sigma AND volume spike AND no obvious scheduled event
    -> trigger investigation agent
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.enums import EventType
from core.logging import get_logger
from market.features import _stdev, price_change_over, zscore

log = get_logger("markets.anomaly")


@dataclass
class AnomalyConfig:
    sigma_threshold: float = 3.0
    volume_spike_ratio: float = 2.0
    liquidity_drop_ratio: float = 0.4
    imbalance_threshold: float = 0.6
    min_price_move: float = 0.02
    scheduled_event_window_min: float = 15.0
    wallet_cluster_window_min: float = 10.0
    min_wallets_simultaneous: int = 3
    divergence_threshold: float = 0.05


@dataclass
class Anomaly:
    kind: str
    severity: float
    market_external_id: str
    market_db_id: int | None = None
    ts: datetime = field(default_factory=utcnow)
    details: dict[str, Any] = field(default_factory=dict)
    requires_investigation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": round(self.severity, 4),
            "market_external_id": self.market_external_id,
            "market_db_id": self.market_db_id,
            "ts": self.ts.isoformat(),
            "details": self.details,
            "requires_investigation": self.requires_investigation,
        }


class AnomalyDetector:
    """Detector stateless: riceve serie e stato, restituisce anomalie."""

    def __init__(self, config: AnomalyConfig | None = None):
        self.config = config or AnomalyConfig()

    def detect_price_anomaly(
        self,
        market_id: str,
        prices: Sequence[tuple[datetime, float]],
        *,
        volumes: Sequence[tuple[datetime, float]] | None = None,
        scheduled_at: datetime | None = None,
        market_db_id: int | None = None,
        now: datetime | None = None,
    ) -> Anomaly | None:
        data = sorted(prices, key=lambda p: p[0])
        if len(data) < 6:
            return None
        values = [value for _, value in data]
        returns = [
            (current - previous) / abs(previous)
            for previous, current in zip(values, values[1:], strict=False)
            if previous
        ]
        if len(returns) < 4:
            return None
        latest_return = returns[-1]
        sigma_score = zscore(latest_return, returns[:-1])
        if sigma_score is None:
            return None
        abs_move = abs(values[-1] - values[-2])
        if abs(sigma_score) < self.config.sigma_threshold or abs_move < self.config.min_price_move:
            return None

        volume_spike, volume_ratio = self._volume_spike(volumes)
        near_scheduled = self._near_scheduled_event(scheduled_at, now=now)
        requires_investigation = volume_spike and not near_scheduled

        severity = min(1.0, abs(sigma_score) / 6.0 + (0.2 if volume_spike else 0.0))
        return Anomaly(
            kind="PRICE_MOVE_SIGMA",
            severity=severity,
            market_external_id=market_id,
            market_db_id=market_db_id,
            ts=now or utcnow(),
            details={
                "sigma": round(sigma_score, 3),
                "return": round(latest_return, 5),
                "abs_move": round(abs_move, 5),
                "volume_spike": volume_spike,
                "volume_ratio": round(volume_ratio, 3) if volume_ratio else None,
                "near_scheduled_event": near_scheduled,
                "price_from": values[-2],
                "price_to": values[-1],
                "move_5m": price_change_over(data, seconds=300, now=now),
            },
            requires_investigation=requires_investigation,
        )

    def detect_volume_anomaly(
        self,
        market_id: str,
        volumes: Sequence[tuple[datetime, float]],
        *,
        market_db_id: int | None = None,
    ) -> Anomaly | None:
        spike, ratio = self._volume_spike(volumes)
        if not spike:
            return None
        return Anomaly(
            kind="VOLUME_SPIKE",
            severity=min(1.0, (ratio or 0) / 10.0),
            market_external_id=market_id,
            market_db_id=market_db_id,
            details={"volume_ratio": round(ratio or 0.0, 3)},
        )

    def detect_liquidity_anomaly(
        self,
        market_id: str,
        liquidity: Sequence[tuple[datetime, float]],
        *,
        market_db_id: int | None = None,
    ) -> Anomaly | None:
        data = sorted(liquidity, key=lambda p: p[0])
        if len(data) < 3:
            return None
        previous = data[-2][1]
        latest = data[-1][1]
        if previous <= 0:
            return None
        change = (latest - previous) / previous
        if change > -self.config.liquidity_drop_ratio:
            return None
        return Anomaly(
            kind="LIQUIDITY_DROP",
            severity=min(1.0, abs(change)),
            market_external_id=market_id,
            market_db_id=market_db_id,
            details={"change": round(change, 4), "from": previous, "to": latest},
        )

    def detect_book_imbalance(
        self,
        market_id: str,
        imbalance: float | None,
        *,
        market_db_id: int | None = None,
        depth: float | None = None,
    ) -> Anomaly | None:
        if imbalance is None or abs(imbalance) < self.config.imbalance_threshold:
            return None
        return Anomaly(
            kind="ORDER_BOOK_IMBALANCE",
            severity=min(1.0, abs(imbalance)),
            market_external_id=market_id,
            market_db_id=market_db_id,
            details={"imbalance": round(imbalance, 4), "depth": depth},
        )

    def detect_wallet_cluster(
        self,
        market_id: str,
        trades: Sequence[dict[str, Any]],
        *,
        qualified_wallets: set[str] | None = None,
        market_db_id: int | None = None,
        now: datetime | None = None,
    ) -> Anomaly | None:
        """Wallet importanti che entrano simultaneamente (sez. 10 e 64)."""
        if not trades:
            return None
        reference = now or utcnow()
        window = timedelta(minutes=self.config.wallet_cluster_window_min)
        recent = [
            trade
            for trade in trades
            if (reference - _ts(trade)) <= window
            and (qualified_wallets is None or trade.get("wallet_address") in qualified_wallets)
        ]
        if not recent:
            return None
        by_direction: dict[str, set[str]] = {}
        exposure: dict[str, float] = {}
        for trade in recent:
            side = str(trade.get("side", "BUY")).upper()
            outcome = str(trade.get("outcome") or "")
            key = f"{side}:{outcome}"
            by_direction.setdefault(key, set()).add(str(trade.get("wallet_address")))
            exposure[key] = exposure.get(key, 0.0) + float(trade.get("usd_size") or 0.0)
        key, wallets = max(by_direction.items(), key=lambda kv: len(kv[1]))
        if len(wallets) < self.config.min_wallets_simultaneous:
            return None
        return Anomaly(
            kind="WALLET_CLUSTER_ENTRY",
            severity=min(1.0, len(wallets) / 10.0),
            market_external_id=market_id,
            market_db_id=market_db_id,
            details={
                "direction": key,
                "wallets": sorted(wallets),
                "n_wallets": len(wallets),
                "exposure_usd": round(exposure.get(key, 0.0), 2),
            },
            requires_investigation=len(wallets) >= self.config.min_wallets_simultaneous + 2,
        )

    def detect_cross_market_divergence(
        self,
        market_a: str,
        probability_a: float,
        market_b: str,
        probability_b: float,
        *,
        market_db_id: int | None = None,
    ) -> Anomaly | None:
        divergence = probability_a - probability_b
        if abs(divergence) < self.config.divergence_threshold:
            return None
        return Anomaly(
            kind="CROSS_MARKET_DIVERGENCE",
            severity=min(1.0, abs(divergence) * 4),
            market_external_id=market_a,
            market_db_id=market_db_id,
            details={
                "market_a": market_a,
                "market_b": market_b,
                "probability_a": probability_a,
                "probability_b": probability_b,
                "divergence": round(divergence, 4),
            },
        )

    def _volume_spike(
        self, volumes: Sequence[tuple[datetime, float]] | None
    ) -> tuple[bool, float | None]:
        if not volumes:
            return False, None
        data = sorted(volumes, key=lambda p: p[0])
        if len(data) < 4:
            return False, None
        deltas = [
            max(0.0, data[i][1] - data[i - 1][1]) for i in range(1, len(data))
        ]
        if len(deltas) < 3:
            return False, None
        baseline = sum(deltas[:-1]) / len(deltas[:-1])
        latest = deltas[-1]
        if baseline <= 0:
            return latest > 0, None
        ratio = latest / baseline
        sigma = _stdev(deltas[:-1])
        spike = ratio >= self.config.volume_spike_ratio or (
            sigma > 0 and (latest - baseline) / sigma >= self.config.sigma_threshold
        )
        return spike, ratio

    def _near_scheduled_event(
        self, scheduled_at: datetime | None, *, now: datetime | None = None
    ) -> bool:
        if scheduled_at is None:
            return False
        reference = now or utcnow()
        delta_min = abs((scheduled_at - reference).total_seconds()) / 60.0
        return delta_min <= self.config.scheduled_event_window_min


def _ts(trade: dict[str, Any]) -> datetime:
    value = trade.get("ts")
    if isinstance(value, datetime):
        return value
    return utcnow()


ANOMALY_EVENT = EventType.ANOMALY_DETECTED
