"""Strategie MVP (patch sez. 31): A Breaking News, B Polymarket signal, C Cross-Asset Lag,
D Macro Release, E Company Event. Registry (sez. 62) e mapping evento -> strategia.

Ogni strategia definisce: shock fattoriali deterministici usati per la
cross-asset confirmation e i candidati "ovvi" (che il comitato puo ribaltare),
orizzonte, reason code, soglie di qualificazione.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.db import session_scope
from core.enums import Category, Factor, MacroIndicator, ReasonCode, SignalType, StrategyStatus
from core.repository import Repository
from core.schemas import DetectedEvent
from market.instrument_registry import InstrumentRegistry


@dataclass(frozen=True)
class StrategyDef:
    strategy_id: str
    signal_type: SignalType
    reason_code: ReasonCode
    description: str
    default_holding_s: int
    max_event_age_s: int
    min_source_reliability: float
    version: str = "1.0.0"
    status: StrategyStatus = StrategyStatus.PAPER
    tags: tuple[str, ...] = field(default_factory=tuple)


STRATEGIES: dict[str, StrategyDef] = {
    "A_BREAKING_NEWS": StrategyDef("A_BREAKING_NEWS", SignalType.BREAKING_NEWS_REPRICING, ReasonCode.NEWS_NOT_FULLY_PRICED, "Breaking news verificata -> causal mapping -> residual alpha -> trade", 900, 1800, 0.7),
    "B_POLYMARKET_SIGNAL": StrategyDef("B_POLYMARKET_SIGNAL", SignalType.POLYMARKET_ASSET_SIGNAL, ReasonCode.POLYMARKET_REPRICING, "Repricing Polymarket -> verifica causa -> asset finanziario impattato", 1800, 3600, 0.6),
    "C_CROSS_ASSET_LAG": StrategyDef("C_CROSS_ASSET_LAG", SignalType.CROSS_ASSET_LAG, ReasonCode.CROSS_ASSET_LAG, "Leader si e' mosso, follower correlato non ancora", 900, 1800, 0.7),
    "D_MACRO_RELEASE": StrategyDef("D_MACRO_RELEASE", SignalType.MACRO_RELEASE, ReasonCode.MACRO_REPRICING, "Actual vs consensus vs previous -> interpretazione -> reazione -> residuo", 900, 900, 0.9),
    "E_COMPANY_EVENT": StrategyDef("E_COMPANY_EVENT", SignalType.COMPANY_EVENT, ReasonCode.COMPANY_EVENT, "Earnings/guidance/M&A/FDA -> CFD societa, settore, correlati", 3600, 3600, 0.75),
    "F_LIMITLESS_MISPRICING": StrategyDef("F_LIMITLESS_MISPRICING", SignalType.LIMITLESS_MISPRICING, ReasonCode.LIMITLESS_MISPRICING, "Model-vs-market su Limitless: probabilita del comitato vs prezzo YES/NO, edge netto dopo fee e spread", 86400, 604800, 0.0),
}


def strategy_for_event(event: DetectedEvent) -> StrategyDef | None:
    if event.kind == "MACRO_RELEASE":
        return STRATEGIES["D_MACRO_RELEASE"]
    if event.kind in ("POLYMARKET_REPRICING", "WALLET_CLUSTER", "ANOMALY"):
        return STRATEGIES["B_POLYMARKET_SIGNAL"]
    if event.kind == "CROSS_ASSET_LAG":
        return STRATEGIES["C_CROSS_ASSET_LAG"]
    if event.kind in ("NEWS", "COMPANY_EVENT"):
        if event.category == Category.COMPANIES:
            return STRATEGIES["E_COMPANY_EVENT"]
        return STRATEGIES["A_BREAKING_NEWS"]
    return None


def factor_shocks_for_event(event: DetectedEvent) -> dict[Factor, float]:
    """Shock fattoriali 'da manuale' (prior deterministico per cross-asset confirmation).

    Il comitato puo dissentire: questo serve solo a misurare se il mercato si sta
    muovendo come l'interpretazione ovvia prevede (patch sez. 8).
    """
    if event.macro is not None:
        return _macro_shocks(event)
    title = event.title.lower()
    shocks: dict[Factor, float] = {}
    if any(k in title for k in ("rate cut", "dovish", "taglio dei tassi", "cuts rates")):
        shocks.update({Factor.RATES: -1.0, Factor.RISK_ON: 0.7, Factor.USD: -0.6, Factor.GOLD: 0.5})
    if any(k in title for k in ("rate hike", "hawkish", "raises rates", "rialzo dei tassi")):
        shocks.update({Factor.RATES: 1.0, Factor.RISK_ON: -0.7, Factor.USD: 0.6, Factor.GOLD: -0.4})
    if any(k in title for k in ("ceasefire", "peace", "truce")):
        shocks.update({Factor.RISK_ON: 0.6, Factor.OIL: -0.5, Factor.GOLD: -0.3})
    if any(k in title for k in ("attack", "strike", "invasion", "escalat", "missile", "war")):
        shocks.update({Factor.RISK_OFF: 0.8, Factor.OIL: 0.6, Factor.GOLD: 0.5, Factor.RISK_ON: -0.6})
    if any(k in title for k in ("tariff", "dazi", "trade war")):
        shocks.update({Factor.RISK_ON: -0.6, Factor.USD: 0.3})
    if any(k in title for k in ("opec", "output cut", "production cut")):
        shocks.update({Factor.OIL: 0.8})
    if any(k in title for k in ("bitcoin", "etf approval", "crypto")):
        shocks.update({Factor.CRYPTO: 0.6})
    if event.polymarket_probability_change is not None and not shocks:
        sign = 1.0 if event.polymarket_probability_change > 0 else -1.0
        if any(k in title for k in ("cut", "lower rates")):
            shocks.update({Factor.RATES: -sign, Factor.RISK_ON: 0.6 * sign, Factor.USD: -0.5 * sign})
        elif any(k in title for k in ("hike", "higher rates")):
            shocks.update({Factor.RATES: sign, Factor.RISK_ON: -0.6 * sign, Factor.USD: 0.5 * sign})
        elif any(k in title for k in ("recession",)):
            shocks.update({Factor.RISK_OFF: sign, Factor.RATES: -0.6 * sign, Factor.RISK_ON: -0.8 * sign})
    return shocks


def _macro_shocks(event: DetectedEvent) -> dict[Factor, float]:
    release = event.macro
    assert release is not None
    surprise = event.surprise
    if surprise is None or surprise == 0:
        return {}
    s = 1.0 if surprise > 0 else -1.0
    ind = release.indicator
    if ind in (MacroIndicator.CPI, MacroIndicator.CORE_CPI, MacroIndicator.PCE):
        # inflazione sopra attese -> tassi su, risk-on giu, USD su, oro giu
        return {Factor.RATES: s, Factor.RISK_ON: -0.7 * s, Factor.USD: 0.6 * s, Factor.GOLD: -0.5 * s}
    if ind in (MacroIndicator.NFP, MacroIndicator.GDP, MacroIndicator.PMI, MacroIndicator.RETAIL_SALES):
        # crescita sopra attese -> tassi su, USD su; equity ambiguo (good news = tassi) -> lieve risk-on
        return {Factor.RATES: 0.7 * s, Factor.USD: 0.5 * s, Factor.RISK_ON: 0.3 * s}
    if ind is MacroIndicator.UNEMPLOYMENT:
        return {Factor.RATES: -0.7 * s, Factor.USD: -0.5 * s, Factor.RISK_ON: -0.4 * s}
    if ind in (MacroIndicator.FOMC, MacroIndicator.ECB, MacroIndicator.BOE, MacroIndicator.BOJ):
        return {Factor.RATES: s, Factor.RISK_ON: -0.6 * s, Factor.USD: 0.5 * s if ind is MacroIndicator.FOMC else -0.3 * s}
    return {}


def obvious_candidates(registry: InstrumentRegistry, shocks: dict[Factor, float], *, top_n: int = 4) -> list[dict[str, Any]]:
    """Candidati 'ovvi' dagli shock fattoriali: input per il comitato, non decisione."""
    scored: list[tuple[float, Any]] = []
    for instrument in registry.all():
        score = sum(instrument.factors.get(f, 0.0) * shock for f, shock in shocks.items())
        if abs(score) >= 0.3:
            scored.append((score, instrument))
    scored.sort(key=lambda pair: -abs(pair[0]))
    return [{"instrument": inst.name, "epic": inst.epic, "direction": "BUY" if score > 0 else "SELL", "factor_score": round(score, 3)} for score, inst in scored[:top_n]]


async def ensure_registry() -> None:
    """Sez. 62: strategy registry persistito."""
    async with session_scope() as session:
        repo = Repository(session)
        for strategy in STRATEGIES.values():
            existing = await repo.get_strategy(strategy.strategy_id)
            await repo.upsert_strategy(
                strategy.strategy_id, version=strategy.version, description=strategy.description,
                status=existing.status if existing else strategy.status.value, capital_limit=existing.capital_limit if existing else 0.0,
                config={"signal_type": strategy.signal_type.value, "reason_code": strategy.reason_code.value, "default_holding_s": strategy.default_holding_s, "max_event_age_s": strategy.max_event_age_s, "min_source_reliability": strategy.min_source_reliability},
            )


async def strategy_enabled(strategy_id: str) -> bool:
    async with session_scope() as session:
        row = await Repository(session).get_strategy(strategy_id)
    return row is not None and row.status != StrategyStatus.DISABLED.value


async def promotion_check(strategy_id: str, *, mode: str, min_trades: int = 100) -> dict[str, Any]:
    """Sez. 34/82: metriche minime prima di promuovere (mai automatico DEMO -> LIVE)."""
    from evaluation.pnl import realized_performance

    perf = await realized_performance(mode=mode)
    strat = perf.get("by_strategy", {}).get(strategy_id, {"n": 0})
    checks = {
        "min_trades": strat.get("n", 0) >= min_trades,
        "positive_expectancy": (strat.get("expectancy") or 0) > 0,
        "positive_roi": (strat.get("roi") or 0) > 0,
        "drawdown_ok": (strat.get("max_drawdown_pct") or 0) < 0.05,
        "profit_factor_ok": (strat.get("profit_factor") or 0) > 1.2,
    }
    return {"strategy_id": strategy_id, "mode": mode, "metrics": strat, "checks": checks, "eligible": all(checks.values()), "note": "la promozione richiede sempre un operatore umano"}
