"""Pipeline con comitato LLM mockato (structured output), event detector, dedup, strategie, quant."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from core.bus import BusEvent
from core.clock import utcnow
from core.config import load_settings
from core.enums import (
    AssetClass,
    Category,
    Direction,
    EventType,
    ExecutionMode,
    Factor,
    MarketStatus,
    SourceTier,
)
from core.repository import Repository
from core.schemas import DetectedEvent, Evidence, Instrument, NewsRecord, Quote
from execution.engine import ExecutionEngine
from execution.paper import PaperBroker
from intelligence.contracts import (
    AnalystThesis,
    FilterOutput,
    InvestigationOutput,
    JudgeDecision,
    RedTeamOutput,
)
from intelligence.event_detector import EventDetector
from intelligence.llm import LLMBudget, LLMClient
from intelligence.pipeline import DecisionPipeline
from market.instrument_registry import InstrumentRegistry
from market.prices import PriceService
from risk.kill_switch import KillSwitch

EPIC = "IX.D.NASDAQ.IFE.IP"
GOLD = "CS.D.CFDGOLD.CFDGC.IP"


def instruments() -> list[Instrument]:
    return [
        Instrument(epic=EPIC, name="US Tech 100", asset_class=AssetClass.INDICES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=1.0, aliases=["nasdaq", "us tech"], factors={Factor.US_EQUITY: 1.0, Factor.RISK_ON: 0.9, Factor.RATES: -0.6}),
        Instrument(epic=GOLD, name="Spot Gold", asset_class=AssetClass.COMMODITIES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=0.3, aliases=["gold", "xauusd"], factors={Factor.GOLD: 1.0, Factor.USD: -0.5, Factor.RATES: -0.5}),
    ]


class FeedProvider:
    name = "test-feed"

    def __init__(self, prices: dict[str, float]):
        self.prices = prices

    async def quote(self, instrument: Instrument) -> Quote:
        p = self.prices[instrument.epic]
        half = (instrument.spread or 0) / 2
        return Quote(epic=instrument.epic, bid=p - half, offer=p + half, ts=utcnow(), market_status=MarketStatus.TRADEABLE, source=self.name)

    async def candles(self, instrument, **kwargs):
        return []

    async def aclose(self):
        return None


def structured(model_obj) -> dict:
    return {"id": "x", "provider": "test", "choices": [{"message": {"role": "assistant", "content": model_obj.model_dump_json()}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "cost": 0.001}}


def committee_transport(judge: JudgeDecision, *, filter_relevant: bool = True, red_verdict: str = "PASS", tool_call_once: bool = True) -> httpx.MockTransport:
    """Simula OpenRouter: risponde per ruolo in base al modello richiesto; il judge prova un tool call."""
    state = {"judge_tool_used": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        model = body["model"]
        if "flash" in model:
            return httpx.Response(200, json=structured(FilterOutput(relevant=filter_relevant, relevance=0.9 if filter_relevant else 0.1, category=Category.MACRO, event_kind="MACRO_RELEASE", is_market_moving=True, likely_assets=["US Tech 100", "Spot Gold"], one_line_summary="CPI sotto attese", reason="dato macro tier-1")))
        if "v4-pro" in model:
            return httpx.Response(200, json=structured(InvestigationOutput(verified=True, verification_notes=["BLS confermato"], catalyst="CPI 2.6 vs 2.8", what_changed_economically="aspettative tassi giu", independent_sources=2, primary_source_tier="TIER_1", first_hypothesis_assets=["US Tech 100"], first_hypothesis_directions=[{"asset": "US Tech 100", "direction": "BUY"}], already_priced_assessment="parziale", confidence=0.8)))
        if "glm" in model or "qwen" in model or "grok" in model:
            decision = "ENTER" if "grok" not in model else "WAIT"
            thesis = AnalystThesis(decision=decision, target_asset="US Tech 100", direction=Direction.BUY, causal_chain=["CPI giu", "tassi giu", "growth equity su"], expected_move_pct=0.006, estimated_probability=0.62, confidence=0.7, already_priced_fraction=0.3, information_credibility=0.95, invalidation_conditions=["prezzo torna sotto pre-release"], summary="long nasdaq")
            return httpx.Response(200, json=structured(thesis))
        if "kimi" in model:
            red = RedTeamOutput(verdict=red_verdict, strongest_case_against="gia prezzato in parte", blocking_reasons=["priced in"] if red_verdict == "BLOCK" else [], critic_score=0.3 if red_verdict == "BLOCK" else 0.7)
            return httpx.Response(200, json=structured(red))
        if "sol-pro" in model:
            # primo turno: tool call (verifica prezzo), secondo: decisione
            has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
            if tool_call_once and not has_tool_result and not state["judge_tool_used"]:
                state["judge_tool_used"] = True
                return httpx.Response(200, json={"id": "x", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "get_ig_price", "arguments": json.dumps({"instrument": "US Tech 100"})}}]}}], "usage": {"prompt_tokens": 2000, "completion_tokens": 50, "cost": 0.004}})
            return httpx.Response(200, json=structured(judge))
        return httpx.Response(500, json={"error": {"message": f"modello non mockato {model}"}})

    return httpx.MockTransport(handler)


def cpi_event() -> DetectedEvent:
    from core.enums import MacroIndicator
    from core.schemas import MacroRelease

    now = utcnow()
    release = MacroRelease(indicator=MacroIndicator.CPI, name="CPI", release_time=now - timedelta(seconds=40), actual=2.6, consensus=2.8, previous=2.9, unit="%", source="BLS")
    return DetectedEvent(event_id="EV-CPI", kind="MACRO_RELEASE", title="US CPI 2.6 vs 2.8", category=Category.MACRO, occurred_at=release.release_time, evidence=[Evidence(evidence_id="e1", type="MACRO_DATA", source="BLS", source_tier=SourceTier.TIER_1, timestamp=release.release_time, reliability=0.97, is_confirmed=True, summary="CPI")], surprise=-0.2, macro=release, source_reliability=0.97, is_verified=True)


async def build(judge: JudgeDecision, *, red_verdict: str = "PASS", autonomy: int = 4, filter_relevant: bool = True):
    settings = load_settings(execution_mode=ExecutionMode.PAPER.value, autonomy_level=autonomy, redis_url=None, risk={"bankroll": 10000.0, "max_stake_abs": 100.0, "max_data_staleness_s": 5.0}, llm={"openrouter_api_key": "test-key", "daily_budget_usd": 100})
    registry = InstrumentRegistry(instruments())
    provider = FeedProvider({EPIC: 20000.0, GOLD: 2400.0})
    prices = PriceService(registry=registry, ig_provider=provider, public_provider=provider, max_staleness_s=5.0)  # type: ignore[arg-type]
    engine = ExecutionEngine(settings=settings, registry=registry, prices=prices, paper=PaperBroker(starting_balance=10000.0), kill_switch=KillSwitch())
    engine.risk_engine._limits = settings.risk
    http = httpx.AsyncClient(transport=committee_transport(judge, red_verdict=red_verdict, filter_relevant=filter_relevant), base_url="https://openrouter.test/api/v1")
    llm = LLMClient(settings.llm, http=http, budget=LLMBudget(settings.llm), persist=True)
    pipeline = DecisionPipeline(engine=engine, llm=llm, prices=prices, registry=registry, settings=settings)
    return pipeline, engine


ENTER = JudgeDecision(decision="ENTER", instrument="US Tech 100", direction=Direction.BUY, stop_distance_pct=0.0025, target_distance_pct=0.005, time_horizon_seconds=900, requested_risk_eur=20.0, expected_move_pct=0.007, already_priced_fraction=0.2, information_credibility=0.95, estimated_probability=0.62, confidence=0.74, causal_interpretation="CPI sotto attese -> tassi giu -> tech su", synthesis_of_committee="GLM e Qwen concordano; il contrarian teme il pricing ma i 2Y non si sono ancora mossi", invalidation_conditions=["prezzo sotto livello pre-release"], explanation=["BLS conferma 2.6 vs 2.8", "Nasdaq +0.1% finora", "residuo netto ~0.5%", "stop sotto pre-release", "R:R 2"], reason_code="MACRO_REPRICING")
PASS = JudgeDecision(decision="PASS", confidence=0.8, synthesis_of_committee="gia prezzato", explanation=["mercato ha gia assorbito"])


async def test_pipeline_end_to_end_esegue_in_paper(engine, bus, memory_cache):
    from strategies.catalog import ensure_registry

    await ensure_registry()
    pipeline, exec_engine = await build(ENTER)
    outcome = await pipeline.handle_event(cpi_event())
    assert outcome.stage == "EXECUTED", outcome.detail
    assert outcome.executed and outcome.proposal is not None
    assert outcome.proposal.epic == EPIC and outcome.proposal.direction is Direction.BUY
    assert outcome.risk_decision.approved
    # size dal rischio: 20 EUR / (20000.5*0.0025 punti * 1) = 0.39998 -> arrotondata per difetto al passo 0.1
    assert outcome.risk_decision.size == pytest.approx(0.3)
    assert outcome.cost_usd > 0
    from core.db import session_scope

    async with session_scope() as session:
        repo = Repository(session)
        journal = await repo.get_journal_entry(outcome.proposal.trade_id)
        assert journal.outcome == "EXECUTED" and journal.portfolio_output["decision"] == "ENTER"
        assert set(journal.analyst_output) == {"causal_analyst", "independent_analyst", "contrarian_agent"}
        assert journal.critic_output["verdict"] == "PASS"
        assert journal.reproducible_inputs["committee"]["final_portfolio_manager"]["tools_used"] == ["get_ig_price"]
        decisions = await repo.recent_llm_decisions(limit=20)
        agents = {d.agent for d in decisions}
        assert {"high_volume_filter", "investigator", "causal_analyst", "independent_analyst", "contrarian_agent", "adversarial_red_team", "final_portfolio_manager"} <= agents
        preds = await repo.unresolved_predictions()
        assert {p.scope for p in preds} >= {"causal_analyst", "final_portfolio_manager", "trade"}
        pos = await repo.get_position(outcome.proposal.trade_id)
        assert pos.status == "OPEN" and pos.stop_level is not None
    assert bus.events_of(EventType.POSITION_OPENED)


async def test_pipeline_judge_pass_non_esegue(engine, bus, memory_cache):
    from strategies.catalog import ensure_registry

    await ensure_registry()
    pipeline, exec_engine = await build(PASS)
    outcome = await pipeline.handle_event(cpi_event())
    assert outcome.stage == "JUDGE_PASS"
    assert not exec_engine.paper.positions
    assert not bus.events_of(EventType.ORDER_SUBMITTED)


async def test_pipeline_filtro_scarta_rumore(engine, bus, memory_cache):
    from strategies.catalog import ensure_registry

    await ensure_registry()
    pipeline, _ = await build(ENTER, filter_relevant=False)
    outcome = await pipeline.handle_event(cpi_event())
    assert outcome.stage == "FILTERED"
    assert outcome.cost_usd < 0.01  # solo il filtro economico


async def test_pipeline_autonomy_2_suggerisce_senza_eseguire(engine, bus, memory_cache):
    from strategies.catalog import ensure_registry

    await ensure_registry()
    pipeline, exec_engine = await build(ENTER, autonomy=2)
    outcome = await pipeline.handle_event(cpi_event())
    assert outcome.stage == "SUGGESTED" and outcome.risk_decision.approved
    assert not exec_engine.paper.positions


async def test_pipeline_risk_kernel_rifiuta_rischio_eccessivo_del_pm(engine, bus, memory_cache):
    """Il PM chiede stop 0.05% con target 0.06% -> R:R 1.2 < 1.5: il kernel rifiuta."""
    from strategies.catalog import ensure_registry

    await ensure_registry()
    bad = ENTER.model_copy(update={"stop_distance_pct": 0.005, "target_distance_pct": 0.006})
    pipeline, exec_engine = await build(bad)
    outcome = await pipeline.handle_event(cpi_event())
    assert outcome.stage == "RISK_REJECTED"
    assert any("reward_risk" in r for r in outcome.risk_decision.rejection_reasons)
    assert not exec_engine.paper.positions
    assert bus.events_of(EventType.TRADE_REJECTED)


async def test_llm_structured_output_retry_su_json_invalido():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{"relevant": "forse"}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        return httpx.Response(200, json=structured(FilterOutput(relevant=True, relevance=0.8)))

    settings = load_settings(redis_url=None, llm={"openrouter_api_key": "k"})
    client = LLMClient(settings.llm, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"), persist=False)
    result = await client.complete("high_volume_filter", [{"role": "user", "content": "test"}], schema=FilterOutput)
    assert calls["n"] == 2 and result.parsed.relevant is True


async def test_llm_schema_via_tool_call_per_modelli_senza_json_schema():
    """GLM 5.3 non supporta response_format: lo schema e' forzato via tool call finale."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert "response_format" not in body
        names = [t["function"]["name"] for t in body["tools"]]
        assert "AnalystThesis" in names
        thesis = AnalystThesis(decision="PASS", estimated_probability=0.5, confidence=0.5, summary="ok")
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "AnalystThesis", "arguments": thesis.model_dump_json()}}]}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    settings = load_settings(redis_url=None, llm={"openrouter_api_key": "k"})
    client = LLMClient(settings.llm, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"), persist=False)
    result = await client.complete("causal_analyst", [{"role": "user", "content": "test"}], schema=AnalystThesis)
    assert result.parsed.decision.value == "PASS"


async def test_llm_tool_non_autorizzato():
    from core.errors import ToolPermissionError
    from intelligence.llm import ToolSpec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "submit_trade", "arguments": "{}"}}]}}], "usage": {}})

    settings = load_settings(redis_url=None, llm={"openrouter_api_key": "k"})
    client = LLMClient(settings.llm, http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"), persist=False)
    tools = [ToolSpec("get_ig_price", "x", {"type": "object", "properties": {}}, lambda: {"ok": 1})]
    with pytest.raises(ToolPermissionError):
        await client.complete("final_portfolio_manager", [{"role": "user", "content": "go"}], tools=tools)


async def test_llm_budget():
    from core.errors import LLMBudgetExceeded

    settings = load_settings(redis_url=None, llm={"openrouter_api_key": "k", "daily_budget_usd": 0.001})
    budget = LLMBudget(settings.llm)
    await budget.record(0.002)
    with pytest.raises(LLMBudgetExceeded):
        await budget.check()


# ------------------------------------------------------------ detector / dedup
async def test_event_detector_news_e_macro(engine, bus, memory_cache):
    detector = EventDetector()
    news = BusEvent(type=EventType.NEWS_DETECTED, payload={"fingerprint": "f1", "title": "Fed cuts rates by 50bp in surprise move", "url": "https://www.federalreserve.gov/x", "source": "Federal Reserve Press", "tier": "TIER_1", "reliability": 0.97, "published_at": utcnow().isoformat(), "categories": ["macro"], "entities": ["Fed"], "is_original": True, "independent_confirmations": 0, "cluster_id": "c1"})
    detected = await detector.on_news(news)
    assert detected is not None and detected.kind == "NEWS" and detected.is_verified
    dup = await detector.on_news(news)
    assert dup is None  # stesso cluster -> nessun nuovo evento
    syndicated = BusEvent(type=EventType.NEWS_DETECTED, payload={**news.payload, "fingerprint": "f2", "is_original": False, "cluster_id": "c1"})
    assert await detector.on_news(syndicated) is None
    old = BusEvent(type=EventType.NEWS_DETECTED, payload={**news.payload, "fingerprint": "f3", "cluster_id": "c3", "published_at": (utcnow() - timedelta(hours=8)).isoformat()})
    assert await detector.on_news(old) is None
    social = BusEvent(type=EventType.NEWS_DETECTED, payload={**news.payload, "fingerprint": "f4", "cluster_id": "c4", "tier": "TIER_5"})
    assert await detector.on_news(social) is None  # social secondario non genera eventi da solo

    from strategies.catalog import factor_shocks_for_event, strategy_for_event

    assert strategy_for_event(detected).strategy_id == "A_BREAKING_NEWS"
    shocks = factor_shocks_for_event(detected)
    assert shocks[Factor.RATES] < 0 and shocks[Factor.RISK_ON] > 0

    macro = await detector.on_macro(BusEvent(type=EventType.MACRO_RELEASE, payload=cpi_event().macro.model_dump(mode="json")))
    assert macro.kind == "MACRO_RELEASE" and macro.surprise == pytest.approx(-0.2)
    assert factor_shocks_for_event(macro)[Factor.RATES] < 0  # CPI sotto attese -> tassi giu
    assert bus.events_of(EventType.EVENT_DETECTED)


def test_news_dedup_cluster():
    from collectors.news.dedup import NewsClusterer, canonical_url, fingerprint

    assert canonical_url("https://www.reuters.com/a/b?utm_source=x") == "reuters.com/a/b"
    assert fingerprint("t", "https://www.reuters.com/a/b?utm_source=x") == fingerprint("t", "https://reuters.com/a/b")
    now = utcnow()
    a = NewsRecord(fingerprint="a", title="Fed cuts interest rates by 50 basis points", url="https://reuters.com/1", source_name="Reuters", tier=SourceTier.TIER_2, published_at=now)
    b = NewsRecord(fingerprint="b", title="Fed cuts interest rates by 50 basis points in surprise move", url="https://cnbc.com/2", source_name="CNBC", tier=SourceTier.TIER_3, published_at=now + timedelta(minutes=2))
    c = NewsRecord(fingerprint="c", title="Fed cuts rates 50bp", url="https://reuters.com/3", source_name="Reuters Business", tier=SourceTier.TIER_2, published_at=now + timedelta(minutes=3))
    d = NewsRecord(fingerprint="d", title="Apple unveils new iPhone lineup", url="https://apple.com/x", source_name="Apple", tier=SourceTier.TIER_1, published_at=now)
    clusterer = NewsClusterer()
    for r in (a, b, c, d):
        clusterer.assign(r)
    assert a.cluster_id == b.cluster_id == c.cluster_id != d.cluster_id
    assert a.is_original and not b.is_original
    info = clusterer.confirmations(a.cluster_id)
    assert info["independent_confirmations"] == 1  # reuters + cnbc = 2 domini -> 1 conferma indipendente
    assert d.is_confirmed  # TIER_1 confermata da sola


def test_quant_residual_e_cross_asset():
    from quant.cross_asset import cross_asset_check, expected_moves_from_factors
    from quant.event_study import build_market_reaction
    from quant.residual_alpha import compute_residual_alpha

    registry = InstrumentRegistry(instruments())
    expected = expected_moves_from_factors(registry, {Factor.RATES: -1.0, Factor.RISK_ON: 0.7})
    assert expected[EPIC] is Direction.BUY and expected[GOLD] is Direction.BUY
    t0 = utcnow() - timedelta(minutes=10)
    nas = [(t0 + timedelta(minutes=i), 20000 + (30 if i >= 5 else 0)) for i in range(11)]  # +0.15% dopo l'evento
    gold = [(t0 + timedelta(minutes=i), 2400 - (5 if i >= 5 else 0)) for i in range(11)]  # contro
    check = cross_asset_check(expected=expected, series_by_epic={EPIC: nas, GOLD: gold}, event_ts=t0 + timedelta(minutes=5), now=utcnow())
    assert check.confirmations == 1 and check.contradictions == 1 and "misti" in check.interpretation
    inst = registry.get(EPIC)
    quote = Quote(epic=EPIC, bid=20029.5, offer=20030.5, source="test", market_status=MarketStatus.TRADEABLE)
    reaction = build_market_reaction(epic=EPIC, series=nas, event_ts=t0 + timedelta(minutes=5), now=utcnow(), expected_move_pct=0.006, direction=Direction.BUY, current_price=20030)
    assert reaction.realized_move == pytest.approx(0.0015, rel=1e-3)
    residual = compute_residual_alpha(instrument=inst, quote=quote, direction=Direction.BUY, expected_move_pct=0.006, reaction=reaction, holding_seconds=900, safety_margin_pct=0.0003, min_net_alpha_pct=0.0005)
    assert residual.residual_move_pct == pytest.approx(0.0045, rel=1e-2)
    assert residual.passes and 0.003 < residual.net_alpha_pct < 0.0045
    priced = compute_residual_alpha(instrument=inst, quote=quote, direction=Direction.BUY, expected_move_pct=0.001, reaction=reaction, holding_seconds=900, safety_margin_pct=0.0003, min_net_alpha_pct=0.0005)
    assert not priced.passes  # gia prezzato


def test_registry_resolve_e_related():
    registry = InstrumentRegistry(instruments())
    assert registry.resolve("Nasdaq").epic == EPIC
    assert registry.resolve("US TECH 100").epic == EPIC
    assert registry.resolve("xauusd").epic == GOLD
    assert registry.resolve("us tech 100 cash") is not None
    related = registry.related(EPIC, min_overlap=0.1)
    assert related and related[0][0].epic == GOLD


def test_sizing_esempio_requisiti():
    """Patch sez. 12: bankroll 500, 0.5% = 2.50, stop 0.50/unit -> 5 unita."""
    from core.config import RiskLimits
    from risk.sizing import compute_size

    inst = Instrument(epic="X", name="X", min_size=1.0, size_step=1.0, value_per_point=1.0, margin_factor=5.0)
    quote = Quote(epic="X", bid=99.9, offer=100.0, source="test", market_status=MarketStatus.TRADEABLE)
    sizing = compute_size(instrument=inst, quote=quote, direction=Direction.BUY, stop_distance=0.5, limits=RiskLimits(max_stake_abs=50), equity=500.0)
    assert sizing.risk_budget_eur == pytest.approx(2.5)
    assert sizing.size == 5.0 and sizing.risk_eur == pytest.approx(2.5)


async def test_event_detector_scarta_rumore_non_finanziario(engine, bus, memory_cache):
    """Sport/intrattenimento/generico non generano eventi: niente spesa LLM su rumore."""
    from core.bus import BusEvent
    from core.enums import EventType
    from intelligence.event_detector import EventDetector

    det = EventDetector()
    now = utcnow()
    base = {"url": "https://x", "published_at": now.isoformat(), "is_original": True, "independent_confirmations": 0}
    # sport tier alto -> scartato
    assert await det.on_news(BusEvent(type=EventType.NEWS_DETECTED, payload={**base, "fingerprint": "s1", "cluster_id": "s1", "title": "Arsenal beats Chelsea 3-0", "source": "BBC", "tier": "TIER_2", "reliability": 0.88, "categories": ["sports"]})) is None
    # cronaca generica senza entita -> scartata
    assert await det.on_news(BusEvent(type=EventType.NEWS_DETECTED, payload={**base, "fingerprint": "g1", "cluster_id": "g1", "title": "five tips to feel at home", "source": "BBC", "tier": "TIER_3", "reliability": 0.72, "categories": ["other"], "entities": []})) is None
    # macro ufficiale -> accettato
    ev = await det.on_news(BusEvent(type=EventType.NEWS_DETECTED, payload={**base, "fingerprint": "m1", "cluster_id": "m1", "title": "Fed signals rate cut as inflation eases", "source": "Federal Reserve", "tier": "TIER_1", "reliability": 0.97, "categories": ["macro"], "entities": ["Fed"]}))
    assert ev is not None and ev.category.value == "macro"
