"""Hard Risk Kernel (sez. 25, patch sez. 12-14, 21, 26-30, 41).

Deterministico. Non contraddice il Judge su questioni di intelligence: verifica
solo i vincoli matematici del conto e del mercato:
    is market tradeable? is data fresh? is order syntactically valid?
    does requested risk exceed hard limit? is margin sufficient?
    would portfolio exposure exceed hard cap? stop presente? R:R minimo?
    residual alpha netto positivo? slippage entro il limite?
Se si -> approva la size; se no -> riduce o rifiuta.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.clock import utcnow
from core.config import RiskLimits, get_settings
from core.enums import Direction, EntryType, ExecutionMode, Factor
from core.errors import KillSwitchActive
from core.logging import get_logger
from core.pricing import effective_leverage, worse_price
from core.schemas import AccountState, RiskCheck, RiskDecision, TradeProposal
from market.instrument_registry import InstrumentRegistry, get_registry
from risk.correlation import OpenExposure, build_exposure, correlated_risk
from risk.kill_switch import KillSwitch, get_kill_switch
from risk.limits import current_limits
from risk.margin import margin_stress_test
from risk.sizing import compute_size

log = get_logger("risk.engine")


class PortfolioContext:
    """Stato del portafoglio necessario alle verifiche (fornito dall'execution engine)."""

    def __init__(
        self,
        *,
        account: AccountState,
        open_positions: list[OpenExposure],
        realized_pnl_today: float,
        realized_pnl_week: float,
        peak_equity_week: float | None,
        trades_today: int,
        rejected_streak: int,
        positions_for_event: dict[str, float] | None = None,
    ):
        self.account = account
        self.open_positions = open_positions
        self.realized_pnl_today = realized_pnl_today
        self.realized_pnl_week = realized_pnl_week
        self.peak_equity_week = peak_equity_week or account.equity
        self.trades_today = trades_today
        self.rejected_streak = rejected_streak
        self.positions_for_event = positions_for_event or {}

    @property
    def open_risk_eur(self) -> float:
        return sum(p.risk_eur for p in self.open_positions)


class RiskEngine:
    def __init__(
        self,
        *,
        limits: RiskLimits | None = None,
        registry: InstrumentRegistry | None = None,
        kill_switch: KillSwitch | None = None,
        mode: ExecutionMode | None = None,
    ):
        self._limits = limits
        self.registry = registry or get_registry()
        self.kill_switch = kill_switch or get_kill_switch()
        self.mode = mode or get_settings().execution_mode

    @property
    def limits(self) -> RiskLimits:
        return self._limits or current_limits()

    def evaluate(self, proposal: TradeProposal, context: PortfolioContext, *, fx_rate_to_eur: float = 1.0) -> RiskDecision:
        limits = self.limits
        checks: list[RiskCheck] = []
        reasons: list[str] = []

        def check(name: str, passed: bool, detail: str = "", value: float | None = None, limit: float | None = None) -> bool:
            checks.append(RiskCheck(name=name, passed=passed, detail=detail, value=value, limit=limit))
            if not passed:
                reasons.append(f"{name}: {detail}")
            return passed

        # 0) kill switch / stato sistema
        try:
            self.kill_switch.guard()
            check("kill_switch", True, "sistema operativo")
        except KillSwitchActive as exc:
            check("kill_switch", False, str(exc))

        instrument = proposal.instrument
        quote = proposal.quote
        equity = context.account.equity

        # 1) mercato tradeable, giurisdizione, feed fresco, prezzo live
        settings = get_settings()
        check("jurisdiction", instrument.epic not in settings.blocked_epics and instrument.asset_class.value not in settings.blocked_asset_classes, "strumento bloccato dalla giurisdizione" if instrument.epic in settings.blocked_epics else "ok")
        check("market_tradeable", quote.market_status.tradeable, f"market_status={quote.market_status.value}")
        age = quote.age_seconds()
        staleness_limit = limits.max_data_staleness_s if quote.source.startswith("ig") else limits.max_public_data_staleness_s
        check("data_fresh", age <= staleness_limit, f"quote age {age:.1f}s > {staleness_limit}s (source={quote.source})", age, staleness_limit)
        check("live_price", quote.bid > 0 and quote.offer >= quote.bid and quote.source != "", f"source={quote.source}")
        if self.mode.uses_real_money:
            check("price_source_is_broker", quote.source.startswith("ig"), f"in LIVE il prezzo deve venire da IG (source={quote.source})")

        # 2) validita sintattica dell'ordine
        check("direction_valid", proposal.direction in (Direction.BUY, Direction.SELL), str(proposal.direction))
        check("entry_type_valid", proposal.entry_type in (EntryType.MARKET, EntryType.LIMIT), proposal.entry_type.value)
        check("stop_present", proposal.stop_distance > 0 or not limits.require_stop, "NO STOP = NO TRADE")
        if instrument.min_stop_distance is not None:
            min_stop = instrument.min_stop_distance
            if instrument.min_stop_distance_unit.upper() == "PERCENTAGE":
                min_stop = quote.mid * instrument.min_stop_distance / 100.0
            check("stop_above_broker_minimum", proposal.stop_distance >= min_stop, f"stop {proposal.stop_distance} < minimo broker {min_stop}", proposal.stop_distance, min_stop)
        if instrument.max_stop_distance is not None:
            check("stop_below_broker_maximum", proposal.stop_distance <= instrument.max_stop_distance, f"stop {proposal.stop_distance} > massimo broker {instrument.max_stop_distance}")
        check("horizon_within_max", proposal.time_horizon_seconds <= limits.max_holding_time_s, f"{proposal.time_horizon_seconds}s > {limits.max_holding_time_s}s", proposal.time_horizon_seconds, limits.max_holding_time_s)

        # 3) reward/risk e residual alpha netto
        rr = proposal.reward_risk_ratio
        if proposal.limit_distance is not None:
            check("reward_risk", rr is not None and rr >= limits.min_reward_risk, f"R:R {rr:.2f} < {limits.min_reward_risk}" if rr is not None else "R:R non calcolabile", rr, limits.min_reward_risk)
        else:
            check("reward_risk", False, "limit_distance/target mancante: R:R non verificabile")
        if proposal.residual is not None:
            net = proposal.residual.net_alpha_pct
            check("net_residual_alpha_positive", net > 0 and net >= limits.min_net_alpha, f"net alpha {net:.5f} < {limits.min_net_alpha}", net, limits.min_net_alpha)
        else:
            check("net_residual_alpha_positive", False, "residual alpha non calcolato")

        # 4) slippage guard sul max_entry
        limit_price = worse_price(quote.price_for(proposal.direction), limits.max_slippage_pct, proposal.direction.value)
        if proposal.direction is Direction.BUY:
            check("max_entry_within_slippage", proposal.max_entry <= limit_price * (1 + 1e-9), f"max_entry {proposal.max_entry} oltre {limit_price:.5f}", proposal.max_entry, limit_price)
        else:
            check("max_entry_within_slippage", proposal.max_entry >= limit_price * (1 - 1e-9), f"max_entry {proposal.max_entry} sotto {limit_price:.5f}", proposal.max_entry, limit_price)

        # 5) perdite giornaliere / settimanali, numero posizioni, streak rifiuti
        daily_loss_limit = equity * limits.max_daily_loss
        check("daily_loss", -context.realized_pnl_today < daily_loss_limit, f"perdita odierna {-context.realized_pnl_today:.2f} >= {daily_loss_limit:.2f}", -context.realized_pnl_today, daily_loss_limit)
        drawdown = (context.peak_equity_week - equity) / context.peak_equity_week if context.peak_equity_week > 0 else 0.0
        check("weekly_drawdown", drawdown < limits.max_weekly_drawdown, f"drawdown settimanale {drawdown:.3%} >= {limits.max_weekly_drawdown:.1%}", drawdown, limits.max_weekly_drawdown)
        check("max_open_positions", len(context.open_positions) < limits.max_open_positions, f"{len(context.open_positions)} posizioni aperte", len(context.open_positions), limits.max_open_positions)
        same_epic = [p for p in context.open_positions if p.epic == instrument.epic]
        check("max_positions_per_asset", len(same_epic) < limits.max_positions_per_asset, f"gia {len(same_epic)} posizioni su {instrument.epic}")
        check("max_trades_per_day", context.trades_today < limits.max_trades_per_day, f"{context.trades_today} trade oggi", context.trades_today, limits.max_trades_per_day)
        check("rejected_streak", context.rejected_streak < limits.max_rejected_orders_streak, f"{context.rejected_streak} rifiuti consecutivi", context.rejected_streak, limits.max_rejected_orders_streak)

        # 6) sizing dal rischio (l'LLM non sceglie mai leva/size)
        risk_fraction = proposal.portfolio.risk_fraction_of_max if proposal.portfolio else 1.0
        try:
            sizing = compute_size(
                instrument=instrument,
                quote=quote,
                direction=proposal.direction,
                stop_distance=proposal.stop_distance,
                limits=limits,
                equity=equity,
                requested_risk_eur=proposal.requested_risk_eur,
                risk_fraction_of_max=risk_fraction,
                limit_distance=proposal.limit_distance,
                probability=proposal.calibrated_probability or proposal.probability,
                fx_rate_to_eur=fx_rate_to_eur,
            )
        except ValueError as exc:
            check("sizing", False, str(exc))
            return RiskDecision(approved=False, checks=checks, rejection_reasons=reasons)

        check("size_positive", sizing.size > 0, f"size {sizing.size} sotto il minimo {instrument.min_size} (loss/unit {sizing.loss_per_unit:.4f} EUR)", sizing.size, instrument.min_size)
        check("risk_per_trade", sizing.risk_eur <= equity * limits.max_risk_per_trade + 1e-9, f"rischio {sizing.risk_eur:.2f} > {equity * limits.max_risk_per_trade:.2f}", sizing.risk_eur, equity * limits.max_risk_per_trade)
        check("risk_abs_cap", sizing.risk_eur <= limits.max_stake_abs + 1e-9, f"rischio {sizing.risk_eur:.2f} > cap {limits.max_stake_abs:.2f}", sizing.risk_eur, limits.max_stake_abs)
        if proposal.requested_risk_eur is not None:
            check("requested_risk_not_exceeded", sizing.risk_eur <= proposal.requested_risk_eur + 1e-6, f"size implica {sizing.risk_eur:.2f} > richiesto {proposal.requested_risk_eur:.2f}")

        # 7) esposizione aggregata, evento, correlazioni, fattori
        open_risk = context.open_risk_eur
        check("max_open_risk", open_risk + sizing.risk_eur <= equity * limits.max_open_risk + 1e-9, f"open risk {open_risk + sizing.risk_eur:.2f} > {equity * limits.max_open_risk:.2f}", open_risk + sizing.risk_eur, equity * limits.max_open_risk)
        event_risk = context.positions_for_event.get(proposal.event_id, 0.0)
        check("max_event_risk", event_risk + sizing.risk_eur <= equity * limits.max_event_risk + 1e-9, f"rischio evento {event_risk + sizing.risk_eur:.2f} > {equity * limits.max_event_risk:.2f}")
        corr_risk, corr_details = correlated_risk(registry=self.registry, new_epic=instrument.epic, new_direction=proposal.direction, positions=context.open_positions)
        check("max_correlated_exposure", corr_risk + sizing.risk_eur <= equity * limits.max_correlated_exposure + 1e-9, f"rischio correlato {corr_risk + sizing.risk_eur:.2f} > {equity * limits.max_correlated_exposure:.2f} ({corr_details})", corr_risk + sizing.risk_eur, equity * limits.max_correlated_exposure)

        new_exposure = OpenExposure(
            epic=instrument.epic, direction=proposal.direction, notional=sizing.notional, risk_eur=sizing.risk_eur,
            asset_class=instrument.asset_class.value, currency=instrument.currency, event_id=proposal.event_id,
            factors=dict(instrument.factors),
        )
        report = build_exposure([*context.open_positions, new_exposure])
        leverage = effective_leverage(report.total_notional, equity)
        check("max_effective_leverage", leverage <= limits.max_effective_leverage, f"leva effettiva {leverage:.2f}x > {limits.max_effective_leverage}x", leverage, limits.max_effective_leverage)
        asset_exp = report.by_epic.get(instrument.epic, 0.0) / equity if equity else 0
        check("max_asset_exposure", asset_exp <= limits.max_asset_exposure, f"nozionale {instrument.epic} {asset_exp:.2f}x equity > {limits.max_asset_exposure}", asset_exp, limits.max_asset_exposure)
        class_exp = report.by_asset_class.get(instrument.asset_class.value, 0.0) / equity if equity else 0
        check("max_asset_class_exposure", class_exp <= limits.max_asset_class_exposure, f"{instrument.asset_class.value} {class_exp:.2f}x equity", class_exp, limits.max_asset_class_exposure)
        ccy_exp = report.by_currency.get(instrument.currency, 0.0) / equity if equity else 0
        check("max_currency_exposure", ccy_exp <= limits.max_currency_exposure, f"{instrument.currency} {ccy_exp:.2f}x equity", ccy_exp, limits.max_currency_exposure)

        # 8) margine e stress test
        stress = margin_stress_test(account=context.account, new_margin=sizing.margin_required, new_risk_eur=sizing.risk_eur, open_risk_eur=open_risk, correlated_risk_eur=corr_risk, limits=limits)
        check("margin_usage", stress.margin_usage_after <= limits.max_margin_usage, f"margin usage {stress.margin_usage_after:.1%} > {limits.max_margin_usage:.0%}", stress.margin_usage_after, limits.max_margin_usage)
        check("min_free_margin", stress.free_margin_ratio_after >= limits.min_free_margin, f"free margin {stress.free_margin_ratio_after:.1%} < {limits.min_free_margin:.0%}", stress.free_margin_ratio_after, limits.min_free_margin)
        worst = min(stress.scenarios.values()) if stress.scenarios else 1.0
        check("margin_stress_scenarios", worst >= limits.stress_min_free_margin, f"scenario peggiore free margin {worst:.1%} < {limits.stress_min_free_margin:.0%}", worst, limits.stress_min_free_margin)

        approved = all(c.passed for c in checks)
        decision = RiskDecision(
            approved=approved,
            size=sizing.size if approved else 0.0,
            risk_eur=sizing.risk_eur if approved else 0.0,
            stop_distance=sizing.stop_distance,
            stop_level=sizing.stop_level,
            limit_distance=sizing.limit_distance,
            limit_level=sizing.limit_level,
            max_entry=proposal.max_entry,
            notional=sizing.notional,
            margin_required=sizing.margin_required,
            effective_leverage=leverage,
            checks=checks,
            rejection_reasons=reasons,
            capped_by=sizing.capped_by,
            stress=stress,
        )
        log.info(
            "risk.decision",
            trade_id=proposal.trade_id,
            approved=approved,
            size=decision.size,
            risk_eur=round(decision.risk_eur, 2),
            failed=[c.name for c in decision.failed_checks],
        )
        return decision

    # ------------------------------------------------------- exposures utils
    @staticmethod
    def exposure_from_positions(rows: list[Any], registry: InstrumentRegistry) -> list[OpenExposure]:
        out: list[OpenExposure] = []
        for row in rows:
            instrument = registry.get(row.epic)
            factors = dict(instrument.factors) if instrument else {Factor(k): float(v) for k, v in (row.factors or {}).items()}
            out.append(
                OpenExposure(
                    epic=row.epic,
                    direction=Direction.parse(row.direction),
                    notional=float(row.notional or 0.0),
                    risk_eur=float(row.risk_eur or 0.0),
                    asset_class=str(row.asset_class or (instrument.asset_class.value if instrument else "OTHER")),
                    currency=str(row.currency or "EUR"),
                    event_id=row.event_id,
                    factors=factors,
                )
            )
        return out

    @staticmethod
    def week_start() -> Any:
        now = utcnow()
        return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
