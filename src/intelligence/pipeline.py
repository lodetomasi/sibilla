"""Decision Pipeline (patch sez. 38):

EVENT -> strategy -> cheap filter -> market context + quant (reaction, residual,
cross-asset) -> investigation -> analisti indipendenti -> red team (se qualificato)
-> JUDGE -> TradeProposal -> HARD RISK KERNEL -> execution -> journal + predictions.

Funnel dei costi: la maggior parte degli eventi muore al filtro o ai check
deterministici (freshness, mercato chiuso, nessun residual alpha plausibile).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from core.bus import emit
from core.clock import utcnow
from core.config import Settings, get_settings
from core.db import session_scope
from core.enums import Direction, EntryType, EventType, ExecutionMode, ReasonCode
from core.errors import LLMBudgetExceeded, LLMError, RiskViolation
from core.logging import get_logger
from core.pricing import worse_price
from core.repository import Repository
from core.schemas import CrossAssetCheck, DetectedEvent, TradeProposal
from execution.engine import ExecutionEngine
from intelligence.committee import Committee, disagreement
from intelligence.contracts import FilterOutput, InvestigationOutput, JudgeDecision, RedTeamOutput
from intelligence.llm import LLMClient, get_llm_client
from intelligence.reliability import (
    record_model_prediction,
    reliability_table,
    weights_for_category,
)
from intelligence.tools import AgentToolbox
from market.instrument_registry import InstrumentRegistry, get_registry
from market.prices import PriceService, get_price_service
from quant.cross_asset import cross_asset_check, expected_moves_from_factors
from quant.event_study import build_market_reaction
from quant.features import build_feature_vector, returns_after
from quant.models import get_quant_model
from quant.residual_alpha import compute_residual_alpha, estimate_costs
from risk.limits import current_limits
from strategies.catalog import (
    StrategyDef,
    factor_shocks_for_event,
    obvious_candidates,
    strategy_enabled,
    strategy_for_event,
)

log = get_logger("intelligence.pipeline")


class PipelineOutcome:
    def __init__(self, event: DetectedEvent, stage: str, *, detail: str = "", proposal: TradeProposal | None = None, decision: Any | None = None, judge: JudgeDecision | None = None, cost_usd: float = 0.0, executed: bool = False):
        self.event = event
        self.stage = stage
        self.detail = detail
        self.proposal = proposal
        self.risk_decision = decision
        self.judge = judge
        self.cost_usd = cost_usd
        self.executed = executed

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event.event_id, "title": self.event.title, "stage": self.stage, "detail": self.detail, "executed": self.executed, "cost_usd": round(self.cost_usd, 4),
                "judge": self.judge.decision if self.judge else None, "epic": self.proposal.epic if self.proposal else None,
                "risk_approved": self.risk_decision.approved if self.risk_decision else None}


class DecisionPipeline:
    def __init__(self, *, engine: ExecutionEngine, llm: LLMClient | None = None, prices: PriceService | None = None, registry: InstrumentRegistry | None = None, settings: Settings | None = None):
        self.engine = engine
        self.llm = llm or get_llm_client()
        self.prices = prices or get_price_service()
        self.registry = registry or get_registry()
        self.settings = settings or get_settings()
        self.quant_model = get_quant_model()
        self.history: list[PipelineOutcome] = []
        self._processing: set[str] = set()

    # ------------------------------------------------------------------ main
    async def handle_event(self, event: DetectedEvent) -> PipelineOutcome:
        if event.event_id in self._processing:
            return PipelineOutcome(event, "SKIPPED", detail="gia in elaborazione")
        self._processing.add(event.event_id)
        try:
            outcome = await self._run(event)
        except LLMBudgetExceeded as exc:
            outcome = PipelineOutcome(event, "BUDGET", detail=str(exc))
        except (LLMError, RiskViolation) as exc:
            outcome = PipelineOutcome(event, "ERROR", detail=str(exc)[:300])
            log.error("pipeline.error", event_id=event.event_id, error=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001
            outcome = PipelineOutcome(event, "ERROR", detail=str(exc)[:300])
            log.exception("pipeline.unexpected", event_id=event.event_id)
        finally:
            self._processing.discard(event.event_id)
        self.history.append(outcome)
        if len(self.history) > 500:
            del self.history[:-500]
        async with session_scope() as session:
            await Repository(session).set_event_status(event.event_id, outcome.stage, impact_map={"outcome": outcome.as_dict()})
        log.info("pipeline.outcome", **outcome.as_dict())
        return outcome

    async def _run(self, event: DetectedEvent) -> PipelineOutcome:
        limits = current_limits()
        strategy = strategy_for_event(event)
        if strategy is None:
            return PipelineOutcome(event, "NO_STRATEGY")
        if not await strategy_enabled(strategy.strategy_id):
            return PipelineOutcome(event, "STRATEGY_DISABLED", detail=strategy.strategy_id)
        if event.age_seconds > strategy.max_event_age_s:
            return PipelineOutcome(event, "STALE_EVENT", detail=f"eta {event.age_seconds:.0f}s > {strategy.max_event_age_s}s")
        if event.source_reliability < strategy.min_source_reliability and not event.is_verified:
            return PipelineOutcome(event, "LOW_RELIABILITY", detail=f"{event.source_reliability:.2f} < {strategy.min_source_reliability}")

        # deterministico: mercato aperto per almeno un candidato?
        shocks = factor_shocks_for_event(event)
        candidates = obvious_candidates(self.registry, shocks) if shocks else []
        universe = self.registry.names()
        quotes = await self.prices.quotes([i.epic for i in self.registry.all()], max_age_s=20)
        tradeable = {epic for epic, q in quotes.items() if q.market_status.tradeable}
        if not tradeable:
            return PipelineOutcome(event, "MARKETS_CLOSED", detail="nessuno strumento tradeable ora")

        signal_id = await self._create_signal(event, strategy, candidates)
        toolbox = AgentToolbox(prices=self.prices, registry=self.registry, engine=self.engine, event_ts=event.occurred_at or event.detected_at)
        committee = Committee(self.llm, tools=toolbox.specs(), signal_id=signal_id)

        # 1) cheap filter
        filtered: FilterOutput = await committee.filter(event, universe=universe)
        if not filtered.relevant or filtered.relevance < self.settings.llm.filter_min_relevance:
            await self._mark_signal(signal_id, "FILTERED")
            return PipelineOutcome(event, "FILTERED", detail=filtered.reason[:200], cost_usd=committee.total_cost)

        # 2) contesto di mercato + quant sui candidati (ovvi + suggeriti dal filtro)
        event_ts = event.occurred_at or event.detected_at
        target_names = list(dict.fromkeys([c["instrument"] for c in candidates] + filtered.likely_assets))
        quant, market_context = await self._quant_context(event, event_ts, target_names, shocks, quotes, strategy)

        # 3) investigazione
        investigation: InvestigationOutput = await committee.investigate(event, filter_output=filtered, market_context=market_context)
        if not investigation.verified and event.source_reliability < 0.9:
            await self._mark_signal(signal_id, "UNVERIFIED")
            return PipelineOutcome(event, "UNVERIFIED", detail="; ".join(investigation.verification_notes)[:300], cost_usd=committee.total_cost)
        # aggiorna quant con gli asset della prima ipotesi
        extra = [a for a in investigation.first_hypothesis_assets if a not in target_names]
        if extra:
            quant, market_context = await self._quant_context(event, event_ts, target_names + extra, shocks, quotes, strategy, investigation=investigation)

        # 4) analisti indipendenti (in parallelo, senza vedersi)
        theses = await committee.analysts(event, investigation=investigation, quant=quant, market_context=market_context, roles=self.settings.llm.analyst_roles)
        for role, thesis in theses.items():
            if thesis is None:
                continue
            epic = self._epic_for(thesis.target_asset)
            await record_model_prediction(scope=role, event_id=event.event_id, trade_id=None, epic=epic, direction=thesis.direction if thesis.decision.value == "ENTER" else None, category=event.category.value, probability=thesis.estimated_probability, expected_move_pct=thesis.expected_move_pct, confidence=thesis.confidence, details={"decision": thesis.decision.value, "summary": thesis.summary[:300]})
        any_enter = any(t and t.decision.value == "ENTER" for t in theses.values())
        best_net = max((v.get("residual", {}).get("net_alpha_pct", -1) for v in quant.get("by_asset", {}).values()), default=-1)
        qualified = any_enter and best_net >= self.settings.llm.qualified_min_net_alpha
        if not any_enter:
            await self._mark_signal(signal_id, "ANALYSTS_PASS")
            await self._journal_reject(event, strategy, signal_id, quant, theses, None, None, committee, "ANALYSTS_PASS")
            return PipelineOutcome(event, "ANALYSTS_PASS", detail="nessun analista propone ENTER", cost_usd=committee.total_cost)

        portfolio = await toolbox.get_portfolio()

        # 5) red team solo su opportunita qualificate
        red: RedTeamOutput | None = None
        if qualified:
            red = await committee.red_team(event, investigation=investigation, theses=theses, quant=quant, portfolio=portfolio)
            await record_model_prediction(scope="adversarial_red_team", event_id=event.event_id, trade_id=None, epic=None, direction=None, category=event.category.value, probability=red.critic_score, expected_move_pct=None, confidence=red.critic_score, details={"verdict": red.verdict.value})

        # 6) judge
        table = await reliability_table()
        reliability = {"weights_hint": weights_for_category(table, event.category.value), "table": {k: v for k, v in table.items()}, "analyst_disagreement": disagreement(theses)}
        equity = portfolio.get("equity", limits.bankroll)
        hard_limits = portfolio.get("hard_limits", {})
        hard_limits.update({"min_net_alpha_pct": limits.min_net_alpha, "max_slippage_pct": limits.max_slippage_pct, "require_stop": limits.require_stop})
        live_prices = {name: {"bid": q.bid, "offer": q.offer, "spread_pct": round(q.spread_pct, 6), "status": q.market_status.value, "age_s": round(q.age_seconds(), 1), "source": q.source} for name, q in ((self.registry.get(e).name, q) for e, q in quotes.items() if self.registry.get(e))}
        judge: JudgeDecision = await committee.judge(event, investigation=investigation, theses=theses, red_team=red, quant=quant, portfolio=portfolio, prices=live_prices, reliability=reliability, hard_limits=hard_limits)
        judge_epic = self._epic_for(judge.instrument) or judge.epic
        await record_model_prediction(scope="final_portfolio_manager", event_id=event.event_id, trade_id=None, epic=judge_epic, direction=judge.direction if judge.enters else None, category=event.category.value, probability=judge.estimated_probability, expected_move_pct=judge.expected_move_pct, confidence=judge.confidence, details={"decision": judge.decision})

        if not judge.enters:
            await self._mark_signal(signal_id, f"JUDGE_{judge.decision}")
            await self._journal_reject(event, strategy, signal_id, quant, theses, red, judge, committee, f"JUDGE_{judge.decision}")
            return PipelineOutcome(event, f"JUDGE_{judge.decision}", detail=judge.synthesis_of_committee[:300], judge=judge, cost_usd=committee.total_cost)

        # 7) TradeProposal dal Judge -> Hard Risk Kernel
        proposal = await self._build_proposal(event, strategy, judge, quant, theses, red, equity)
        if proposal is None:
            await self._mark_signal(signal_id, "NO_INSTRUMENT")
            return PipelineOutcome(event, "NO_INSTRUMENT", detail=f"strumento '{judge.instrument}' non risolto o non tradeable", judge=judge, cost_usd=committee.total_cost)
        decision = await self.engine.assess(proposal)
        await self._journal(event, strategy, signal_id, proposal, decision, quant, theses, red, judge, committee)
        if not decision.approved:
            await self._mark_signal(signal_id, "RISK_REJECTED", trade_id=proposal.trade_id)
            await emit(EventType.TRADE_REJECTED, {"trade_id": proposal.trade_id, "epic": proposal.epic, "reasons": decision.rejection_reasons[:5]}, source="pipeline")
            return PipelineOutcome(event, "RISK_REJECTED", detail="; ".join(decision.rejection_reasons)[:300], proposal=proposal, decision=decision, judge=judge, cost_usd=committee.total_cost)

        await emit(EventType.TRADE_APPROVED, {"trade_id": proposal.trade_id, "epic": proposal.epic, "direction": proposal.direction.value, "size": decision.size, "risk_eur": decision.risk_eur}, source="pipeline")
        if self.settings.autonomy_level.value < 3:
            await self._mark_signal(signal_id, "SUGGESTED", trade_id=proposal.trade_id)
            async with session_scope() as session:
                await Repository(session).update_journal_entry(proposal.trade_id, outcome="SUGGESTED_NOT_EXECUTED")
            return PipelineOutcome(event, "SUGGESTED", detail=f"autonomy level {self.settings.autonomy_level.value}: nessuna esecuzione automatica", proposal=proposal, decision=decision, judge=judge, cost_usd=committee.total_cost)

        # 8) esecuzione
        result = await self.engine.submit(proposal, decision, quote=proposal.quote)
        executed = result.status == "FILLED"
        async with session_scope() as session:
            repo = Repository(session)
            await repo.update_journal_entry(proposal.trade_id, execution_result=result.model_dump(mode="json"), outcome="EXECUTED" if executed else f"EXEC_{result.status}", entry_price=result.fill_price, size=result.filled_size, price_source=proposal.quote.source)
        await self._mark_signal(signal_id, "EXECUTED" if executed else "EXEC_FAILED", trade_id=proposal.trade_id)
        # prediction del trade (per calibrazione ex post, sez. 37)
        await record_model_prediction(scope="trade", event_id=event.event_id, trade_id=proposal.trade_id, epic=proposal.epic, direction=proposal.direction, category=event.category.value, probability=proposal.probability, expected_move_pct=proposal.expected_return_pct, confidence=proposal.confidence)
        return PipelineOutcome(event, "EXECUTED" if executed else "EXEC_FAILED", detail=result.error or f"fill {result.fill_price} size {result.filled_size}", proposal=proposal, decision=decision, judge=judge, cost_usd=committee.total_cost, executed=executed)

    # ------------------------------------------------------------- quant
    async def _quant_context(self, event: DetectedEvent, event_ts: Any, names: list[str], shocks: dict, quotes: dict, strategy: StrategyDef, *, investigation: InvestigationOutput | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utcnow()
        limits = current_limits()
        series_by_epic: dict[str, list] = {}
        for inst in self.registry.all():
            series_by_epic[inst.epic] = await self.prices.price_series(inst.epic, since=event_ts - timedelta(hours=2))
        expected_dirs = expected_moves_from_factors(self.registry, shocks) if shocks else {}
        cross: CrossAssetCheck | None = cross_asset_check(expected=expected_dirs, series_by_epic=series_by_epic, event_ts=event_ts, now=now) if expected_dirs else None
        by_asset: dict[str, Any] = {}
        for name in names:
            inst = self.registry.resolve(name)
            if inst is None:
                continue
            quote = quotes.get(inst.epic)
            if quote is None:
                continue
            direction = expected_dirs.get(inst.epic)
            if direction is None and investigation is not None:
                direction = investigation.direction_for(name) or investigation.direction_for(inst.name)
            if direction is None:
                direction = Direction.BUY
            series = series_by_epic.get(inst.epic, [])
            pred = self.quant_model.predict(category=event.category, kind=event.kind, surprise_sigma=None, source_reliability=event.source_reliability, freshness_weight=event.freshness.weight, cross_asset_score=cross.score if cross else None, direction=direction)
            reaction = build_market_reaction(epic=inst.epic, series=series, event_ts=event_ts, now=now, expected_move_pct=pred.expected_move_pct, direction=direction, current_price=quote.mid, data_source=quote.source)
            residual = compute_residual_alpha(instrument=inst, quote=quote, direction=direction, expected_move_pct=pred.expected_move_pct, reaction=reaction, holding_seconds=strategy.default_holding_s, safety_margin_pct=limits.safety_margin_alpha, min_net_alpha_pct=limits.min_net_alpha)
            by_asset[inst.name] = {
                "epic": inst.epic, "prior_direction": direction.value, "quant": pred.as_dict(),
                "reaction": {k: v for k, v in reaction.model_dump(mode="json").items() if k not in ("epic",)},
                "residual": {"expected_move_pct": residual.expected_move_pct, "realized_move_pct": residual.realized_move_pct, "residual_move_pct": residual.residual_move_pct, "costs_pct": residual.costs.total_pct, "net_alpha_pct": residual.net_alpha_pct, "passes": residual.passes},
                "quote": {"bid": quote.bid, "offer": quote.offer, "spread_pct": round(quote.spread_pct, 6), "status": quote.market_status.value, "source": quote.source},
                "returns_since_event": {k: v for k, v in returns_after(series, event_ts, reaction.price_before_event or quote.mid, now=now).items() if v is not None},
            }
        quant = {"by_asset": by_asset, "factor_shocks_prior": {k.value: v for k, v in shocks.items()}, "cross_asset": cross.model_dump(mode="json") if cross else None, "note": "prior deterministico: il comitato puo dissentire"}
        market_context = {"event_ts": event_ts.isoformat(), "now": now.isoformat(), "tradeable_now": [self.registry.get(e).name for e, q in quotes.items() if q.market_status.tradeable and self.registry.get(e)], "price_source": next(iter(quotes.values())).source if quotes else None}
        return quant, market_context

    # ----------------------------------------------------------- proposal
    async def _build_proposal(self, event: DetectedEvent, strategy: StrategyDef, judge: JudgeDecision, quant: dict[str, Any], theses: dict, red: RedTeamOutput | None, equity: float) -> TradeProposal | None:
        inst = self.registry.resolve(judge.instrument or "") or (self.registry.get(judge.epic) if judge.epic else None)
        if inst is None or judge.direction is None:
            return None
        quote = await self.prices.quote(inst.epic, max_age_s=2.0)
        entry = quote.price_for(judge.direction)
        stop_pct = float(judge.stop_distance_pct or 0.0)
        target_pct = float(judge.target_distance_pct or 0.0)
        stop_distance = entry * stop_pct
        limit_distance = entry * target_pct if target_pct else None
        limits = current_limits()
        max_slip = min(judge.max_entry_slippage_pct, limits.max_slippage_pct)
        asset_quant = quant.get("by_asset", {}).get(inst.name, {})
        residual_data = asset_quant.get("residual", {})
        event_ts = event.occurred_at or event.detected_at
        series = await self.prices.price_series(inst.epic, since=event_ts - timedelta(hours=2))
        expected = float(judge.expected_move_pct or residual_data.get("expected_move_pct") or 0.0)
        reaction = build_market_reaction(epic=inst.epic, series=series, event_ts=event_ts, now=utcnow(), expected_move_pct=expected, direction=judge.direction, current_price=quote.mid, data_source=quote.source)
        residual = compute_residual_alpha(instrument=inst, quote=quote, direction=judge.direction, expected_move_pct=expected, reaction=reaction, holding_seconds=judge.time_horizon_seconds, safety_margin_pct=limits.safety_margin_alpha, min_net_alpha_pct=limits.min_net_alpha)
        costs = estimate_costs(inst, quote, holding_seconds=judge.time_horizon_seconds)
        reason_code = ReasonCode(judge.reason_code) if judge.reason_code in ReasonCode.__members__ else strategy.reason_code
        red_score = red.critic_score if red else None
        features = build_feature_vector(event_surprise=event.surprise, source_reliability=event.source_reliability, source_freshness_s=event.age_seconds, polymarket_probability_change=event.polymarket_probability_change, asset_returns=asset_quant.get("returns_since_event", {}), spread_pct=quote.spread_pct, volatility_pct=reaction.volatility_pct, liquidity_proxy=None, cross_asset_confirmation=(quant.get("cross_asset") or {}).get("score"), expected_move=expected, observed_move=reaction.realized_move, residual_move=residual.residual_move_pct, llm_confidence=judge.confidence, critic_score=red_score)
        explanation = judge.explanation or [judge.causal_interpretation[:200]]
        pre_event = reaction.price_before_event
        return TradeProposal(
            trade_id=f"T{utcnow().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}", event_id=event.event_id, strategy_id=strategy.strategy_id, signal_type=strategy.signal_type,
            instrument=inst, epic=inst.epic, direction=judge.direction, entry_type=judge.entry_type if judge.entry_type in (EntryType.MARKET, EntryType.LIMIT) else EntryType.MARKET,
            quote=quote, max_entry=worse_price(entry, max_slip, judge.direction.value), max_slippage_pct=max_slip, stop_distance=stop_distance, limit_distance=limit_distance,
            stop_rationale="; ".join(judge.invalidation_conditions)[:300], time_horizon_seconds=min(judge.time_horizon_seconds, limits.max_holding_time_s),
            expected_return_pct=expected, expected_loss_pct=stop_pct, probability=judge.estimated_probability, confidence=judge.confidence,
            requested_risk_eur=judge.requested_risk_eur, reason_code=reason_code, residual=residual, cross_asset=CrossAssetCheck.model_validate(quant["cross_asset"]) if quant.get("cross_asset") else None,
            invalidation_conditions=judge.invalidation_conditions + judge.early_exit_conditions, evidence=event.evidence, explanation=explanation[:5],
            features={**features, "costs_pct": costs.total_pct, "pre_event_price": pre_event or 0.0},
        )

    # ------------------------------------------------------------ persist
    async def _create_signal(self, event: DetectedEvent, strategy: StrategyDef, candidates: list[dict[str, Any]]) -> int:
        async with session_scope() as session:
            signal = await Repository(session).add_signal(signal_type=strategy.signal_type.value, strategy_id=strategy.strategy_id, event_id=event.event_id, candidate_assets=candidates, evidence=[e.model_dump(mode="json") for e in event.evidence], features={"source_reliability": event.source_reliability, "age_s": event.age_seconds}, status="NEW")
            return signal.id

    async def _mark_signal(self, signal_id: int, status: str, *, trade_id: str | None = None) -> None:
        async with session_scope() as session:
            await Repository(session).set_signal_status(signal_id, status, **({"trade_id": trade_id} if trade_id else {}))

    async def _journal(self, event: DetectedEvent, strategy: StrategyDef, signal_id: int, proposal: TradeProposal, decision: Any, quant: dict, theses: dict, red: RedTeamOutput | None, judge: JudgeDecision, committee: Committee) -> None:
        async with session_scope() as session:
            await Repository(session).add_journal_entry(
                trade_id=proposal.trade_id, mode=self.engine.mode.value, strategy_id=strategy.strategy_id, signal_type=strategy.signal_type.value, event_id=event.event_id, event_title=event.title,
                epic=proposal.epic, instrument_name=proposal.instrument.name, direction=proposal.direction.value, entry_price=proposal.quote.price_for(proposal.direction), size=decision.size, risk_eur=decision.risk_eur,
                stop_level=decision.stop_level, limit_level=decision.limit_level, time_horizon_seconds=proposal.time_horizon_seconds, expected_move_pct=proposal.expected_return_pct,
                realized_move_pct=proposal.residual.realized_move_pct if proposal.residual else None, residual_alpha_pct=proposal.residual.residual_move_pct if proposal.residual else None,
                net_alpha_pct=proposal.residual.net_alpha_pct if proposal.residual else None, costs_pct=proposal.residual.costs.total_pct if proposal.residual else None,
                probability=proposal.probability, llm_probability=judge.estimated_probability, confidence=proposal.confidence, features=proposal.features,
                evidence=[e.model_dump(mode="json") for e in event.evidence], source_ids=[e.evidence_id for e in event.evidence], impact_map={"quant": quant, "candidates_prior": quant.get("by_asset", {}).keys().__len__()},
                analyst_output={k: (v.model_dump(mode="json") if v else None) for k, v in theses.items()}, critic_output=red.model_dump(mode="json") if red else {}, portfolio_output=judge.model_dump(mode="json"),
                risk_decision=decision.model_dump(mode="json"), explanation=proposal.explanation, latencies={"committee_ms": sum(r.latency_ms for r in committee.results.values())},
                invalidation_conditions=proposal.invalidation_conditions, outcome="APPROVED" if decision.approved else "REJECTED_RISK", price_source=proposal.quote.source,
                reproducible_inputs={"event": event.model_dump(mode="json"), "committee": committee.transcript(), "prompt_version": self.settings.llm.prompt_version, "mode": self.engine.mode.value, "limits": current_limits().model_dump()},
            )

    async def _journal_reject(self, event: DetectedEvent, strategy: StrategyDef, signal_id: int, quant: dict, theses: dict, red: RedTeamOutput | None, judge: JudgeDecision | None, committee: Committee, outcome: str) -> None:
        async with session_scope() as session:
            await Repository(session).add_journal_entry(
                trade_id=f"N{utcnow().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}", mode=self.engine.mode.value, strategy_id=strategy.strategy_id, signal_type=strategy.signal_type.value,
                event_id=event.event_id, event_title=event.title, epic=self._epic_for(judge.instrument) if judge and judge.instrument else None, direction=judge.direction.value if judge and judge.direction else None,
                analyst_output={k: (v.model_dump(mode="json") if v else None) for k, v in theses.items()}, critic_output=red.model_dump(mode="json") if red else {}, portfolio_output=judge.model_dump(mode="json") if judge else {},
                impact_map={"quant": quant}, outcome=outcome, evidence=[e.model_dump(mode="json") for e in event.evidence],
                reproducible_inputs={"event": event.model_dump(mode="json"), "committee": committee.transcript(), "prompt_version": self.settings.llm.prompt_version},
            )

    def _epic_for(self, name: str | None) -> str | None:
        if not name:
            return None
        inst = self.registry.resolve(name) or self.registry.get(name)
        return inst.epic if inst else None

    @property
    def mode(self) -> ExecutionMode:
        return self.engine.mode
