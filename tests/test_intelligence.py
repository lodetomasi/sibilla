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
from intelligence.contracts import (
    AnalystThesis,
    FilterOutput,
    InvestigationOutput,
    JudgeDecision,
    RedTeamOutput,
)
from intelligence.event_detector import EventDetector
from intelligence.llm import LLMBudget, LLMClient
from market.instrument_registry import InstrumentRegistry

EPIC = "IX.D.NASDAQ.IFE.IP"
GOLD = "CS.D.CFDGOLD.CFDGC.IP"


def instruments() -> list[Instrument]:
    return [
        Instrument(epic=EPIC, name="US Tech 100", asset_class=AssetClass.INDICES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=1.0, aliases=["nasdaq", "us tech"], factors={Factor.US_EQUITY: 1.0, Factor.RISK_ON: 0.9, Factor.RATES: -0.6}),
        Instrument(epic=GOLD, name="Spot Gold", asset_class=AssetClass.COMMODITIES, currency="USD", min_size=0.1, size_step=0.1, value_per_point=1.0, margin_factor=5.0, spread=0.3, aliases=["gold", "xauusd"], factors={Factor.GOLD: 1.0, Factor.USD: -0.5, Factor.RATES: -0.5}),
    ]


def structured(model_obj) -> dict:
    return {"id": "x", "provider": "test", "choices": [{"message": {"role": "assistant", "content": model_obj.model_dump_json()}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "cost": 0.001}}


def cpi_event() -> DetectedEvent:
    from core.enums import MacroIndicator
    from core.schemas import MacroRelease

    now = utcnow()
    release = MacroRelease(indicator=MacroIndicator.CPI, name="CPI", release_time=now - timedelta(seconds=40), actual=2.6, consensus=2.8, previous=2.9, unit="%", source="BLS")
    return DetectedEvent(event_id="EV-CPI", kind="MACRO_RELEASE", title="US CPI 2.6 vs 2.8", category=Category.MACRO, occurred_at=release.release_time, evidence=[Evidence(evidence_id="e1", type="MACRO_DATA", source="BLS", source_tier=SourceTier.TIER_1, timestamp=release.release_time, reliability=0.97, is_confirmed=True, summary="CPI")], surprise=-0.2, macro=release, source_reliability=0.97, is_verified=True)


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

    macro = await detector.on_macro(BusEvent(type=EventType.MACRO_RELEASE, payload=cpi_event().macro.model_dump(mode="json")))
    assert macro.kind == "MACRO_RELEASE" and macro.surprise == pytest.approx(-0.2)
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
