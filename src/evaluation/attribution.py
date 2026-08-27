"""Alpha attribution (patch sez. 36) e ablation (sez. 59)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.db import session_scope
from core.enums import AlphaSource
from core.repository import Repository

_SIGNAL_SOURCE = {
    "STRATEGY_A_BREAKING_NEWS": AlphaSource.NEWS,
    "STRATEGY_B_POLYMARKET_SIGNAL": AlphaSource.POLYMARKET,
    "STRATEGY_C_CROSS_ASSET_LAG": AlphaSource.CROSS_ASSET,
    "STRATEGY_D_MACRO_RELEASE": AlphaSource.MACRO,
    "STRATEGY_E_COMPANY_EVENT": AlphaSource.NEWS,
}


def attribute_trade(entry: Any) -> dict[str, float]:
    """Ripartisce il P&L di un trade fra le fonti di alpha.

    Regola: 45% alla fonte del segnale, 20% LLM (decisione PM), 15% timing
    (quota di expected move catturata prima dell'entrata), 10% cross-asset se
    confermato, 10% execution (slippage vs riferimento). Le quote non usate
    tornano alla fonte primaria.
    """
    pnl = float(entry.pnl or 0.0)
    if pnl == 0:
        return {}
    primary = _SIGNAL_SOURCE.get(entry.signal_type or "", AlphaSource.LLM)
    shares: dict[AlphaSource, float] = {primary: 0.45, AlphaSource.LLM: 0.20, AlphaSource.TIMING: 0.15}
    features = entry.features or {}
    if features.get("cross_asset_confirmation", 0) > 0.3:
        shares[AlphaSource.CROSS_ASSET] = shares.get(AlphaSource.CROSS_ASSET, 0.0) + 0.10
    else:
        shares[primary] += 0.10
    exec_result = entry.execution_result or {}
    slip = exec_result.get("slippage_pct")
    if slip is not None:
        shares[AlphaSource.EXECUTION] = 0.10 if float(slip) <= 0 else -0.05
        shares[primary] += 0.0 if float(slip) <= 0 else 0.15
    else:
        shares[primary] += 0.10
    if entry.signal_type == "STRATEGY_B_POLYMARKET_SIGNAL" and (entry.features or {}).get("wallet_signal"):
        shares[AlphaSource.WALLET] = 0.10
        shares[primary] -= 0.10
    total = sum(shares.values())
    return {source.value: round(pnl * share / total, 4) for source, share in shares.items()} if total else {}


async def attribution_report(*, mode: str | None = None, since: datetime | None = None) -> dict[str, Any]:
    async with session_scope() as session:
        repo = Repository(session)
        entries = [e for e in await repo.journal_entries(mode=mode, since=since, limit=10000) if e.pnl is not None]
        totals: dict[str, float] = {}
        for entry in entries:
            breakdown = attribute_trade(entry)
            await repo.update_journal_entry(entry.trade_id, attribution=breakdown)
            for source, value in breakdown.items():
                totals[source] = totals.get(source, 0.0) + value
    total_pnl = sum(float(e.pnl or 0.0) for e in entries)
    return {"total_pnl": round(total_pnl, 2), "by_source": {k: round(v, 2) for k, v in sorted(totals.items(), key=lambda kv: -abs(kv[1]))}, "n_trades": len(entries), "method": "signal_weight"}


async def ablation_report(*, mode: str | None = None, since: datetime | None = None) -> dict[str, Any]:
    """Sez. 59: performance per configurazione (quant only / LLM only / full) usando i tag nel journal."""
    async with session_scope() as session:
        entries = [e for e in await Repository(session).journal_entries(mode=mode, since=since, limit=10000) if e.pnl is not None]
    groups: dict[str, list[float]] = {"full_system": []}
    for e in entries:
        groups["full_system"].append(float(e.pnl))
        inputs = e.reproducible_inputs or {}
        variant = inputs.get("ablation_variant")
        if variant:
            groups.setdefault(variant, []).append(float(e.pnl))
        # contro-fattuali: se il red team aveva BLOCK ma il PM e' entrato, ecc.
        critic = e.critic_output or {}
        if critic.get("verdict") == "BLOCK":
            groups.setdefault("pm_overrode_red_team", []).append(float(e.pnl))
        if (e.analyst_output or {}).get("contrarian", {}).get("decision") in ("WAIT", "PASS"):
            groups.setdefault("contrarian_disagreed", []).append(float(e.pnl))
    return {k: {"n": len(v), "pnl": round(sum(v), 2), "mean": round(sum(v) / len(v), 3) if v else None, "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3) if v else None} for k, v in groups.items()}
