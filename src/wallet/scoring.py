"""Wallet scoring per categoria (sez. 5.1, 5.3, 64) con point-in-time (sez. 6)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.enums import Category
from core.repository import Repository
from core.schemas import WalletMetrics, WalletScoreCard
from wallet.profiler import compute_wallet_metrics

MIN_SAMPLE = 15


def score_from_metrics(m: WalletMetrics) -> tuple[float, dict[str, float]]:
    """Score 0-1: combina ROI, win rate, payoff, drawdown, Sharpe, CLV con shrinkage sul campione."""
    n = m.n_trades
    if n == 0:
        return 0.0, {}
    shrink = n / (n + MIN_SAMPLE)
    roi_c = _squash(m.roi, scale=0.3)
    wr_c = max(0.0, min(1.0, (m.win_rate - 0.5) * 2 + 0.5))
    payoff_c = _squash(m.payoff_ratio - 1.0, scale=1.0)
    pf_c = _squash(m.profit_factor - 1.0, scale=1.0)
    dd_c = 1.0 - min(1.0, m.max_drawdown / (abs(m.realized_pnl) + m.max_drawdown + 1e-9))
    sharpe_c = _squash(m.sharpe_like, scale=2.0)
    clv_c = _squash(m.clv_edge, scale=0.05)
    components = {"roi": roi_c, "win_rate": wr_c, "payoff": payoff_c, "profit_factor": pf_c, "drawdown": dd_c, "sharpe": sharpe_c, "clv": clv_c, "shrink": shrink}
    raw = 0.2 * roi_c + 0.15 * wr_c + 0.15 * payoff_c + 0.1 * pf_c + 0.1 * dd_c + 0.15 * sharpe_c + 0.15 * clv_c
    return raw * shrink, components


def _squash(x: float, *, scale: float) -> float:
    return 1 / (1 + math.exp(-x / scale)) if scale else 0.5


class WalletScorer:
    """Calcola e persiste score per wallet x categoria, con as_of esplicito."""

    def __init__(self, *, lookback_days: int = 180):
        self.lookback_days = lookback_days

    async def score_wallet(self, address: str, *, as_of: datetime | None = None, categories: list[str] | None = None) -> list[WalletScoreCard]:
        as_of = as_of or utcnow()
        since = as_of - timedelta(days=self.lookback_days)
        async with session_scope() as session:
            repo = Repository(session)
            trades = list(await repo.wallet_trades(address, since=since, until=as_of))
        if not trades:
            return []
        cats = categories or ["ALL", *sorted({t.category for t in trades})]
        cards: list[WalletScoreCard] = []
        async with session_scope() as session:
            repo = Repository(session)
            for category in cats:
                metrics = compute_wallet_metrics(address, trades, category=category, since=since, until=as_of)
                if metrics.n_trades == 0:
                    continue
                score, components = score_from_metrics(metrics)
                card = WalletScoreCard(address=address, category=category, as_of=as_of, score=score, sample_size=metrics.n_trades, metrics=metrics, components=components)
                cards.append(card)
                await repo.save_wallet_score(
                    wallet_address=address, category=category, as_of=as_of, window_start=since, window_end=as_of, score=score,
                    roi=metrics.roi, win_rate=metrics.win_rate, pnl=metrics.realized_pnl, max_drawdown=metrics.max_drawdown,
                    sample_size=metrics.n_trades, clv_edge=metrics.clv_edge, metrics=metrics.model_dump(mode="json"),
                )
        return cards

    async def score_all(self, *, as_of: datetime | None = None, limit: int = 2000) -> int:
        async with session_scope() as session:
            wallets = list(await Repository(session).list_wallets(limit=limit))
        total = 0
        for wallet in wallets:
            total += len(await self.score_wallet(wallet.address, as_of=as_of))
        return total


async def qualified_wallets(*, category: Category | str = "ALL", as_of: datetime | None = None, min_score: float = 0.55, min_sample: int = MIN_SAMPLE, limit: int = 100) -> list[dict[str, Any]]:
    """Wallet 'forti' in una categoria, usando solo score calcolati prima di as_of."""
    cat = category.value if isinstance(category, Category) else category
    async with session_scope() as session:
        rows = await Repository(session).top_wallets(category=cat, as_of=as_of, min_sample=min_sample, limit=limit * 3)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.wallet_address in seen or row.score < min_score:
            continue
        seen.add(row.wallet_address)
        out.append({"address": row.wallet_address, "score": row.score, "roi": row.roi, "win_rate": row.win_rate, "sample_size": row.sample_size, "as_of": row.as_of})
        if len(out) >= limit:
            break
    return out


def wallet_consensus(trades: list[Any], qualified: dict[str, float], *, window_minutes: int = 15, now: datetime | None = None) -> dict[str, Any] | None:
    """Sez. 64: piu wallet qualificati nella stessa direzione -> segnale pesato."""
    reference = now or utcnow()
    recent = [t for t in trades if (reference - t.ts) <= timedelta(minutes=window_minutes) and t.wallet_address in qualified]
    if not recent:
        return None
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for t in recent:
        key = (str(t.condition_id), str(t.outcome), str(t.side).upper())
        bucket = by_key.setdefault(key, {"wallets": set(), "exposure": 0.0, "weight": 0.0})
        bucket["wallets"].add(t.wallet_address)
        bucket["exposure"] += float(t.usd_size or 0.0)
        bucket["weight"] += qualified[t.wallet_address]
    key, best = max(by_key.items(), key=lambda kv: kv[1]["weight"])
    total_weight = sum(b["weight"] for b in by_key.values())
    return {
        "condition_id": key[0], "outcome": key[1], "side": key[2], "n_wallets": len(best["wallets"]),
        "wallets": sorted(best["wallets"]), "exposure_usd": round(best["exposure"], 2),
        "weighted_signal": round(best["weight"] / total_weight, 3) if total_weight else 0.0,
    }
