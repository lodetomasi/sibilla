"""Wallet persistence (sez. 6): l'edge persiste nel tempo? Walk-forward train/validation/test.

Il ranking di ogni finestra usa SOLO dati precedenti alla finestra valutata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from core.db import session_scope
from core.repository import Repository
from wallet.profiler import compute_wallet_metrics
from wallet.scoring import score_from_metrics


@dataclass
class PersistenceResult:
    address: str
    category: str
    windows: list[dict[str, Any]] = field(default_factory=list)
    persistence_score: float = 0.0
    ranking_stability: float = 0.0
    rolling_roi: list[float] = field(default_factory=list)
    rolling_sharpe: list[float] = field(default_factory=list)
    rolling_win_rate: list[float] = field(default_factory=list)
    rolling_payoff: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address, "category": self.category, "persistence_score": round(self.persistence_score, 4),
            "ranking_stability": round(self.ranking_stability, 4), "rolling_roi": self.rolling_roi, "rolling_sharpe": self.rolling_sharpe,
            "rolling_win_rate": self.rolling_win_rate, "rolling_payoff": self.rolling_payoff, "windows": self.windows,
        }


def split_windows(start: datetime, end: datetime, *, train_days: int = 90, test_days: int = 60) -> list[tuple[datetime, datetime, datetime]]:
    """[(train_start, split, test_end), ...] rolling."""
    windows: list[tuple[datetime, datetime, datetime]] = []
    cursor = start
    while cursor + timedelta(days=train_days + test_days) <= end:
        split = cursor + timedelta(days=train_days)
        windows.append((cursor, split, split + timedelta(days=test_days)))
        cursor = cursor + timedelta(days=test_days)
    return windows


async def evaluate_persistence(address: str, *, category: str = "ALL", start: datetime, end: datetime, train_days: int = 90, test_days: int = 60) -> PersistenceResult:
    async with session_scope() as session:
        trades = list(await Repository(session).wallet_trades(address, since=start, until=end))
    result = PersistenceResult(address=address, category=category)
    prev_score: float | None = None
    stability_terms: list[float] = []
    for train_start, split, test_end in split_windows(start, end, train_days=train_days, test_days=test_days):
        train = compute_wallet_metrics(address, trades, category=category, since=train_start, until=split)
        test = compute_wallet_metrics(address, trades, category=category, since=split, until=test_end)
        if train.n_trades < 5 or test.n_trades < 3:
            continue
        train_score, _ = score_from_metrics(train)
        test_score, _ = score_from_metrics(test)
        persisted = (train.roi > 0) == (test.roi > 0) and test.roi > 0
        result.windows.append({"train": [train_start.isoformat(), split.isoformat()], "test": [split.isoformat(), test_end.isoformat()], "train_roi": round(train.roi, 4), "test_roi": round(test.roi, 4), "train_score": round(train_score, 4), "test_score": round(test_score, 4), "persisted": persisted})
        result.rolling_roi.append(round(test.roi, 4))
        result.rolling_sharpe.append(round(test.sharpe_like, 3))
        result.rolling_win_rate.append(round(test.win_rate, 3))
        result.rolling_payoff.append(round(test.payoff_ratio, 3))
        if prev_score is not None:
            stability_terms.append(1.0 - min(1.0, abs(test_score - prev_score)))
        prev_score = test_score
    if result.windows:
        result.persistence_score = sum(1 for w in result.windows if w["persisted"]) / len(result.windows)
    if stability_terms:
        result.ranking_stability = sum(stability_terms) / len(stability_terms)
    async with session_scope() as session:
        repo = Repository(session)
        latest = await repo.wallet_score(address, category)
        if latest is not None:
            latest.persistence_score = result.persistence_score
            latest.metrics = {**(latest.metrics or {}), "persistence": result.as_dict()}
    return result
