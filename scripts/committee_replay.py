"""REPLAY di un evento macro REALE attraverso l'intero comitato (modelli veri su OpenRouter).

Prende l'ultimo dato CPI USA pubblicato dal BLS (API pubblica, nessuna chiave), lo
trasforma in un MACRO_RELEASE come se fosse uscito 60 secondi fa e lo fa attraversare
filtro -> investigator -> 3 analisti indipendenti -> red team -> judge -> risk kernel -> PAPER.

Serve a verificare che i 7 modelli producano output strutturati validi e che il kernel
li vincoli. NON e' una misura di alpha: il timestamp e' spostato (dichiarato nel journal).
Usa un DB separato (data/ats_replay.db) per non contaminare le statistiche PAPER.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ATS_DATABASE_URL", f"sqlite+aiosqlite:///{ROOT}/data/ats_replay.db")
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from collectors.base import CollectionMode  # noqa: E402
from core.clock import utcnow  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.enums import Category, EvidenceType, MacroIndicator, SourceTier  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.repository import Repository  # noqa: E402
from core.schemas import DetectedEvent, Evidence, MacroRelease  # noqa: E402
from workers.runner import Runner  # noqa: E402

BLS_SERIES = {"CPI": "CUSR0000SA0", "CORE_CPI": "CUSR0000SA0L1E"}


async def fetch_bls(series_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}")
        r.raise_for_status()
        data = r.json()["Results"]["series"][0]["data"]
    return sorted(data, key=lambda d: (int(d["year"]), int(d["period"][1:])))


async def build_event() -> DetectedEvent:
    cpi = await fetch_bls(BLS_SERIES["CPI"])
    core = await fetch_bls(BLS_SERIES["CORE_CPI"])
    last, prev, prev2 = cpi[-1], cpi[-2], cpi[-3]
    mom = (float(last["value"]) / float(prev["value"]) - 1) * 100
    mom_prev = (float(prev["value"]) / float(prev2["value"]) - 1) * 100
    yoy = (float(last["value"]) / float(cpi[-13]["value"]) - 1) * 100
    core_mom = (float(core[-1]["value"]) / float(core[-2]["value"]) - 1) * 100
    month = f"{last['periodName']} {last['year']}"
    now = utcnow()
    release_time = now - timedelta(seconds=60)
    release = MacroRelease(indicator=MacroIndicator.CPI, name=f"US CPI {month} (REPLAY del dato BLS reale)", country="US", release_time=release_time, actual=round(mom, 2), consensus=None, previous=round(mom_prev, 2), unit="% m/m", source="BLS API v1 CUSR0000SA0 (replay)", url="https://www.bls.gov/cpi/")
    summary = f"CPI {month}: {mom:+.2f}% m/m (prev {mom_prev:+.2f}%), {yoy:.1f}% y/y; core {core_mom:+.2f}% m/m. Consensus non disponibile: sorpresa misurata vs previous."
    evidence = Evidence(evidence_id=f"bls-cpi-{last['year']}-{last['period']}", type=EvidenceType.MACRO_DATA, source="BLS", source_tier=SourceTier.TIER_1, url="https://www.bls.gov/cpi/", timestamp=release_time, reliability=0.97, impact=0.8, is_confirmed=True, summary=summary, details={"index": last["value"], "prev_index": prev["value"], "yoy": round(yoy, 2), "core_mom": round(core_mom, 2), "replay": True})
    return DetectedEvent(event_id=f"REPLAY-CPI-{last['year']}{last['period']}-{now.strftime('%H%M')}", kind="MACRO_RELEASE", title=f"[REPLAY] US CPI {month}: {mom:+.2f}% m/m vs prev {mom_prev:+.2f}% ({yoy:.1f}% y/y)", summary=summary, category=Category.MACRO, occurred_at=release_time, evidence=[evidence], entities=["CPI", "US", "Fed"], surprise=round(mom - mom_prev, 3), macro=release, source_reliability=0.97, is_verified=True, raw={"replay": True, "note": "timestamp spostato a ora-60s per esercitare il comitato"})


async def main() -> None:
    configure_logging("WARNING")
    runner = Runner()
    await runner.setup()
    assert runner.pipeline and runner.prices
    print(f"== setup: mode={runner.settings.execution_mode.value} autonomy={runner.settings.autonomy_level.value} ig={runner.ig_client is not None} db={os.environ['ATS_DATABASE_URL'].split('/')[-1]}", flush=True)
    await runner.collectors["ig_prices"].run_once(CollectionMode.HISTORICAL_BATCH, minutes=6 * 60)
    await runner.collectors["ig_prices"].run_once()
    tradeable = [i.name for i in runner.registry.all() if (q := runner.prices.cached(i.epic)) and q.market_status.tradeable and q.age_seconds() < 150]
    print(f"== strumenti con prezzo fresco e tradeable ora: {tradeable}", flush=True)
    event = await build_event()
    print(f"\n== EVENTO: {event.title}\n   {event.summary}", flush=True)
    started = utcnow()
    outcome = await runner.pipeline.handle_event(event)
    elapsed = (utcnow() - started).total_seconds()
    print(f"\n== ESITO ({elapsed:.0f}s, ${outcome.cost_usd:.4f}): {outcome.stage} - {outcome.detail[:300]}", flush=True)
    async with session_scope() as session:
        repo = Repository(session)
        decisions = await repo.recent_llm_decisions(limit=12)
        print("\n== COMITATO:")
        total = 0.0
        for d in reversed(decisions):
            total += d.cost_usd
            print(f"   {d.agent:26s} {d.model:30s} {d.latency_ms/1000:6.1f}s in={d.input_tokens:6d} out={d.output_tokens:5d} ${d.cost_usd:.4f} tools={d.tools_used} err={(d.error or '')[:60]}", flush=True)
            out = d.structured_output or {}
            key = next((k for k in ("decision", "verdict", "relevant", "verified") if k in out), None)
            if key:
                extra = {k: out.get(k) for k in ("target_asset", "direction", "expected_move_pct", "estimated_probability", "confidence", "critic_score", "instrument", "requested_risk_eur", "stop_distance_pct", "target_distance_pct", "already_priced_fraction") if out.get(k) is not None}
                print(f"      -> {key}={out[key]} {extra}")
                for field in ("summary", "synthesis_of_committee", "strongest_case_against", "catalyst", "reason"):
                    if out.get(field):
                        print(f"      {field}: {str(out[field])[:260]}")
                        break
        print(f"   COSTO TOTALE ${total:.4f}")
        if outcome.judge:
            print("\n== JUDGE (decisione finale):", json.dumps({k: v for k, v in outcome.judge.model_dump(mode='json').items() if k in ('decision', 'instrument', 'direction', 'stop_distance_pct', 'target_distance_pct', 'time_horizon_seconds', 'requested_risk_eur', 'expected_move_pct', 'already_priced_fraction', 'confidence', 'explanation', 'invalidation_conditions', 'synthesis_of_committee')}, indent=1, ensure_ascii=False))
        if outcome.risk_decision:
            rd = outcome.risk_decision
            print(f"\n== RISK KERNEL: approved={rd.approved} size={rd.size} risk_eur={rd.risk_eur:.2f} stop={rd.stop_level} limit={rd.limit_level} notional={rd.notional:.0f} margin={rd.margin_required:.0f} leva={rd.effective_leverage:.2f}x")
            print("   check falliti:", [(c.name, c.detail[:80]) for c in rd.failed_checks])
        positions = await repo.open_positions(runner.settings.execution_mode.value)
        for p in positions:
            print(f"\n== POSIZIONE PAPER: {p.instrument_name} {p.direction} size={p.size} entry={p.entry_price} stop={p.stop_level} limit={p.limit_level} risk={p.risk_eur} time-stop={p.max_holding_until}")
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
