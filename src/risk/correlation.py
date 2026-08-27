"""Portfolio correlation / factor exposure (patch sez. 29)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.enums import Direction, Factor
from market.instrument_registry import InstrumentRegistry, correlation_proxy


@dataclass
class OpenExposure:
    epic: str
    direction: Direction
    notional: float
    risk_eur: float
    asset_class: str
    currency: str
    event_id: str | None = None
    factors: dict[Factor, float] = field(default_factory=dict)


@dataclass
class ExposureReport:
    factor_exposure: dict[Factor, float]  # nozionale firmato per fattore
    factor_risk: dict[Factor, float]  # rischio EUR firmato per fattore
    by_asset_class: dict[str, float]
    by_currency: dict[str, float]
    by_epic: dict[str, float]
    total_notional: float
    total_risk_eur: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor_exposure": {k.value: round(v, 2) for k, v in self.factor_exposure.items()},
            "factor_risk_eur": {k.value: round(v, 2) for k, v in self.factor_risk.items()},
            "by_asset_class": {k: round(v, 2) for k, v in self.by_asset_class.items()},
            "by_currency": {k: round(v, 2) for k, v in self.by_currency.items()},
            "by_epic": {k: round(v, 2) for k, v in self.by_epic.items()},
            "total_notional": round(self.total_notional, 2),
            "total_risk_eur": round(self.total_risk_eur, 2),
        }


def build_exposure(positions: Iterable[OpenExposure]) -> ExposureReport:
    factor_exposure: dict[Factor, float] = {}
    factor_risk: dict[Factor, float] = {}
    by_class: dict[str, float] = {}
    by_ccy: dict[str, float] = {}
    by_epic: dict[str, float] = {}
    total_notional = total_risk = 0.0
    for position in positions:
        sign = position.direction.sign
        for factor, loading in position.factors.items():
            factor_exposure[factor] = factor_exposure.get(factor, 0.0) + sign * loading * position.notional
            factor_risk[factor] = factor_risk.get(factor, 0.0) + sign * loading * position.risk_eur
        by_class[position.asset_class] = by_class.get(position.asset_class, 0.0) + position.notional
        by_ccy[position.currency] = by_ccy.get(position.currency, 0.0) + position.notional
        by_epic[position.epic] = by_epic.get(position.epic, 0.0) + position.notional
        total_notional += position.notional
        total_risk += position.risk_eur
    return ExposureReport(factor_exposure, factor_risk, by_class, by_ccy, by_epic, total_notional, total_risk)


def correlated_risk(
    *, registry: InstrumentRegistry, new_epic: str, new_direction: Direction, positions: Iterable[OpenExposure],
    min_corr: float = 0.4,
) -> tuple[float, list[dict[str, Any]]]:
    """Rischio EUR delle posizioni aperte che si muovono insieme al nuovo trade.

    "LONG Nasdaq + LONG S&P + LONG Nvidia" = un unico trade risk-on (patch sez. 29).
    """
    base = registry.factor_vector(new_epic)
    total = 0.0
    details: list[dict[str, Any]] = []
    for position in positions:
        corr = correlation_proxy(base, position.factors)
        effective = corr * new_direction.sign * position.direction.sign
        if effective >= min_corr:
            total += position.risk_eur * effective
            details.append({"epic": position.epic, "direction": position.direction.value, "corr": round(effective, 3), "risk_eur": round(position.risk_eur, 2)})
    return total, details


def dominant_factor_exposure(report: ExposureReport, equity: float) -> dict[str, float]:
    """Esposizione fattoriale relativa all'equity (per il PM e la dashboard)."""
    if equity <= 0:
        return {}
    return {k.value: round(v / equity, 3) for k, v in sorted(report.factor_exposure.items(), key=lambda kv: -abs(kv[1]))}
