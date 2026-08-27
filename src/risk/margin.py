"""Margin stress test (patch sez. 28) e leverage guard (sez. 27)."""
from __future__ import annotations

from core.config import RiskLimits
from core.schemas import AccountState, MarginStress


def margin_stress_test(
    *,
    account: AccountState,
    new_margin: float,
    new_risk_eur: float,
    open_risk_eur: float,
    correlated_risk_eur: float,
    limits: RiskLimits,
) -> MarginStress:
    """Simula margine e free margin dopo il trade e negli scenari -1R/-2R/correlati."""
    equity = account.equity
    margin_after = account.margin_used + new_margin
    free_after = equity - margin_after
    free_ratio_after = free_after / equity if equity > 0 else 0.0
    usage_after = margin_after / equity if equity > 0 else 1.0

    scenarios: dict[str, float] = {}
    for r in limits.stress_scenarios_r:
        loss = new_risk_eur * r
        eq = equity - loss
        scenarios[f"-{r:g}R"] = (eq - margin_after) / eq if eq > 0 else -1.0
    # tutte le posizioni correlate colpiscono lo stop insieme al nuovo trade
    loss_corr = new_risk_eur + correlated_risk_eur
    eq_corr = equity - loss_corr
    scenarios["correlated_stops"] = (eq_corr - margin_after) / eq_corr if eq_corr > 0 else -1.0
    # tutte le posizioni aperte colpiscono lo stop
    loss_all = new_risk_eur + open_risk_eur
    eq_all = equity - loss_all
    scenarios["all_stops"] = (eq_all - margin_after) / eq_all if eq_all > 0 else -1.0

    passes = (
        usage_after <= limits.max_margin_usage
        and free_ratio_after >= limits.min_free_margin
        and all(ratio >= limits.stress_min_free_margin for ratio in scenarios.values())
    )
    return MarginStress(
        equity=equity,
        margin_used_before=account.margin_used,
        margin_required=new_margin,
        margin_used_after=margin_after,
        free_margin_after=free_after,
        free_margin_ratio_after=free_ratio_after,
        margin_usage_after=usage_after,
        scenarios={k: round(v, 4) for k, v in scenarios.items()},
        passes=passes,
    )
