"""Contract test REALE per Red Team (Kimi K3) e Final PM (GPT-5.6 Sol Pro).

Costruisce un caso di comitato plausibile sull'ultimo CPI BLS reale (tesi analisti
SINTETICHE, dichiarate) e chiama davvero red team + judge con i tool, poi passa la
decisione al Risk Kernel. Verifica: schemi strutturati accettati dai provider, tool use,
coerenza stop/target/rischio, esito del kernel. DB separato (ats_replay.db).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ATS_DATABASE_URL", f"sqlite+aiosqlite:///{ROOT}/data/ats_replay.db")
sys.path.insert(0, str(ROOT / "src"))

from committee_replay import build_event  # noqa: E402

from collectors.base import CollectionMode  # noqa: E402
from core.clock import utcnow  # noqa: E402
from core.enums import AnalystDecision, Direction  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from intelligence.committee import Committee  # noqa: E402
from intelligence.contracts import AnalystThesis, InvestigationOutput  # noqa: E402
from intelligence.tools import AgentToolbox  # noqa: E402
from workers.runner import Runner  # noqa: E402


async def main() -> None:
    configure_logging("WARNING")
    runner = Runner()
    await runner.setup()
    assert runner.pipeline and runner.prices and runner.engine
    await runner.collectors["ig_prices"].run_once(CollectionMode.HISTORICAL_BATCH, minutes=6 * 60)
    quotes = await runner.collectors["ig_prices"].run_once()
    print(f"== setup: mode={runner.settings.execution_mode.value} quotes={quotes}", flush=True)
    event = await build_event()
    toolbox = AgentToolbox(prices=runner.prices, registry=runner.registry, engine=runner.engine, event_ts=event.occurred_at)
    committee = Committee(runner.pipeline.llm, tools=toolbox.specs())
    investigation = InvestigationOutput(verified=True, verification_notes=["[TEST] dato BLS reale, timestamp spostato"], catalyst=event.title, what_changed_economically="rimbalzo CPI headline dopo un mese negativo; core +0.22% m/m", surprise_description="vs previous +0.49pp (consensus n/d)", independent_sources=1, primary_source_tier="TIER_1", first_hypothesis_assets=["EUR/USD", "Spot Gold"], first_hypothesis_directions=[{"asset": "EUR/USD", "direction": "SELL"}, {"asset": "Spot Gold", "direction": "SELL"}], already_priced_assessment="[TEST] da verificare con i tool", confidence=0.6)
    theses = {
        "causal_analyst": AnalystThesis(analyst="causal_analyst", decision=AnalystDecision.ENTER, target_asset="EUR/USD", direction=Direction.SELL, causal_chain=["CPI sopra previous", "tagli Fed rinviati", "rendimenti USD su", "EUR/USD giu"], expected_move_pct=0.003, time_horizon_seconds=1800, estimated_probability=0.58, confidence=0.6, already_priced_fraction=0.3, information_credibility=0.95, invalidation_conditions=["EUR/USD torna sopra il livello pre-release"], summary="[TEST SINTETICO] short EUR/USD su repricing hawkish"),
        "independent_analyst": AnalystThesis(analyst="independent_analyst", decision=AnalystDecision.WAIT, estimated_probability=0.5, confidence=0.4, summary="[TEST SINTETICO] attendere conferma cross-asset"),
        "contrarian_agent": AnalystThesis(analyst="contrarian_agent", decision=AnalystDecision.PASS, estimated_probability=0.35, confidence=0.7, already_priced_fraction=1.0, summary="[TEST SINTETICO] mercato fuori orario, reazione gia assorbita"),
    }
    quant = runner.pipeline and (await runner.pipeline._quant_context(event, event.occurred_at, ["EUR/USD", "Spot Gold", "US Tech 100"], {}, {e: runner.prices.cached(e) for e in [i.epic for i in runner.registry.all()] if runner.prices.cached(e)}, __import__("strategies.catalog", fromlist=["STRATEGIES"]).STRATEGIES["D_MACRO_RELEASE"], investigation=investigation))[0]
    portfolio = await toolbox.get_portfolio()
    started = utcnow()
    red = await committee.red_team(event, investigation=investigation, theses=theses, quant=quant, portfolio=portfolio)
    print(f"\n== RED TEAM (Kimi K3) {(utcnow()-started).total_seconds():.0f}s ${committee.costs.get('adversarial_red_team', 0):.4f} tools={committee.results['adversarial_red_team'].tools_used}", flush=True)
    print(json.dumps(red.model_dump(mode="json"), indent=1, ensure_ascii=False)[:2500], flush=True)
    started = utcnow()
    judge = await committee.judge(event, investigation=investigation, theses=theses, red_team=red, quant=quant, portfolio=portfolio, prices={i.name: {"bid": q.bid, "offer": q.offer, "status": q.market_status.value, "age_s": round(q.age_seconds())} for i in runner.registry.all() if (q := runner.prices.cached(i.epic))}, reliability={"note": "nessuna storia ancora"}, hard_limits=portfolio.get("hard_limits", {}))
    print(f"\n== JUDGE (Sol Pro) {(utcnow()-started).total_seconds():.0f}s ${committee.costs.get('final_portfolio_manager', 0):.4f} tools={committee.results['final_portfolio_manager'].tools_used}", flush=True)
    print(json.dumps(judge.model_dump(mode="json"), indent=1, ensure_ascii=False)[:3500], flush=True)
    if judge.enters:
        proposal = await runner.pipeline._build_proposal(event, __import__("strategies.catalog", fromlist=["STRATEGIES"]).STRATEGIES["D_MACRO_RELEASE"], judge, quant, theses, red, portfolio["equity"])
        if proposal:
            decision = await runner.engine.assess(proposal)
            print(f"\n== RISK KERNEL: approved={decision.approved} size={decision.size} risk_eur={decision.risk_eur:.2f} stop={decision.stop_level} limit={decision.limit_level} leva={decision.effective_leverage:.2f}x", flush=True)
            print("   falliti:", [(c.name, c.detail[:90]) for c in decision.failed_checks], flush=True)
    print(f"\n== COSTO TOTALE ${committee.total_cost:.4f}", flush=True)
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
