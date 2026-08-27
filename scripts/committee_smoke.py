"""Smoke end-to-end REALE: prezzi reali, news reali, comitato LLM reale su OpenRouter, execution PAPER.

Prende l'evento piu fresco e affidabile disponibile (news Tier 1-2 o repricing Polymarket)
e lo fa attraversare l'intera pipeline una volta, stampando esiti, costi e journal.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collectors.base import CollectionMode  # noqa: E402
from core.bus import BusEvent  # noqa: E402
from core.clock import utcnow  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.enums import EventType  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.repository import Repository  # noqa: E402
from workers.runner import Runner  # noqa: E402


async def main() -> None:
    configure_logging("INFO")
    runner = Runner()
    await runner.setup()
    assert runner.pipeline and runner.prices
    print(f"\n== setup ok: mode={runner.settings.execution_mode.value} autonomy={runner.settings.autonomy_level.value} ig={runner.ig_client is not None} strumenti={len(runner.registry.all())}")
    n_prices = await runner.collectors["ig_prices"].run_once(CollectionMode.HISTORICAL_BATCH, minutes=6 * 60)
    live = await runner.collectors["ig_prices"].run_once()
    print(f"== prezzi: storico {n_prices} punti, live {live} quote, fonte {runner.prices.live_source}")
    for inst in runner.registry.all():
        q = runner.prices.cached(inst.epic)
        if q:
            print(f"   {inst.name:22s} bid={q.bid:<12.5g} offer={q.offer:<12.5g} {q.market_status.value:10s} age={q.age_seconds():.0f}s")
    n_news = await runner.collectors["news_rss"].run_once()
    print(f"== news nuove raccolte: {n_news} (feed ok: {runner.collectors['news_rss'].stats.details.get('feeds_ok')}, falliti: {runner.collectors['news_rss'].stats.details.get('feeds_failed')})")
    try:
        n_pm = await runner.collectors["polymarket_markets"].run_once(limit=60, with_books=False)
        print(f"== mercati Polymarket aggiornati: {n_pm}")
    except Exception as exc:  # noqa: BLE001
        print(f"== Polymarket non raggiungibile: {str(exc)[:160]}")

    # scegli l'evento: news piu fresca con tier alto e categoria macro/economics/geopolitics/companies
    async with session_scope() as session:
        news = await Repository(session).recent_news(minutes=240, limit=300)
    ranked = sorted([n for n in news if n.is_original and n.tier in ("TIER_1", "TIER_2", "TIER_3") and any(c in (n.categories or []) for c in ("macro", "economics", "geopolitics", "companies", "crypto", "politics"))], key=lambda n: (n.tier, -(n.published_at or n.retrieved_at).timestamp()))
    if not ranked:
        print("nessuna news candidata nelle ultime 4 ore")
        await runner.shutdown()
        return
    tier_rank = {"TIER_1": 0, "TIER_2": 1, "TIER_3": 2}
    ranked.sort(key=lambda n: (tier_rank.get(n.tier, 3), 0 if any(c in (n.categories or []) for c in ("macro", "economics")) else 1, -(n.published_at or n.retrieved_at).timestamp()))
    outcome = None
    for chosen in ranked[:10]:
        print(f"\n== candidato: [{chosen.tier}] {chosen.source_name}: {chosen.title[:110]}\n   {chosen.url}\n   pubblicata {chosen.published_at} categorie {chosen.categories}")
        runner.detector._seen.discard(chosen.cluster_id)
        payload = {"fingerprint": chosen.fingerprint, "title": chosen.title, "url": chosen.url, "source": chosen.source_name, "tier": chosen.tier, "reliability": chosen.reliability, "published_at": utcnow().isoformat(), "categories": chosen.categories, "entities": chosen.entities, "is_original": True, "independent_confirmations": chosen.independent_confirmations, "cluster_id": chosen.cluster_id}
        detected = await runner.detector.on_news(BusEvent(type=EventType.NEWS_DETECTED, payload=payload))
        if detected is None:
            continue
        started = utcnow()
        outcome = await runner.pipeline.handle_event(detected)
        elapsed = (utcnow() - started).total_seconds()
        print(f"   -> {outcome.stage} ({elapsed:.0f}s, ${outcome.cost_usd:.4f}): {outcome.detail[:160]}")
        if outcome.stage not in ("FILTERED", "STALE_EVENT", "LOW_RELIABILITY", "NO_STRATEGY", "STRATEGY_DISABLED"):
            break
    if outcome is None:
        print("nessun evento processabile")
        await runner.shutdown()
        return
    print(f"\n== ESITO PIPELINE: {json.dumps(outcome.as_dict(), indent=1, default=str)}")
    if outcome.judge:
        print("\n== JUDGE:", json.dumps(outcome.judge.model_dump(mode='json'), indent=1, ensure_ascii=False, default=str)[:3500])
    if outcome.risk_decision:
        print("\n== RISK:", json.dumps({k: v for k, v in outcome.risk_decision.model_dump(mode='json').items() if k != 'checks'}, indent=1, default=str)[:1500])
        print("   check falliti:", [c.name for c in outcome.risk_decision.failed_checks])
    async with session_scope() as session:
        repo = Repository(session)
        decisions = await repo.recent_llm_decisions(limit=20)
        print("\n== CHIAMATE LLM:")
        total = 0.0
        for d in reversed(decisions):
            total += d.cost_usd
            print(f"   {d.agent:26s} {d.model:32s} {d.latency_ms/1000:6.1f}s in={d.input_tokens:6d} out={d.output_tokens:5d} ${d.cost_usd:.4f} tools={d.tools_used} err={d.error or ''}")
        print(f"   TOTALE ${total:.4f}")
        positions = await repo.open_positions(runner.settings.execution_mode.value)
        print(f"\n== posizioni aperte: {len(positions)}")
        for p in positions:
            print(f"   {p.instrument_name} {p.direction} size={p.size} entry={p.entry_price} stop={p.stop_level} limit={p.limit_level} risk={p.risk_eur} until={p.max_holding_until}")
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
