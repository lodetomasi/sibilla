"""Dimostrazione ONESTA della catena d'acquisto su un mercato APERTO ORA.

Non inventa notizie: prende il movimento di prezzo REALE piu marcato nell'ultima
ora tra gli strumenti aperti (crypto 24/7, forex), lo trasforma in un evento
'market anomaly + cross-asset' e lo fa passare per il comitato LLM VERO, il risk
kernel e l'execution PAPER. Se il comitato entra, apre una posizione reale (simulata
su prezzi veri) visibile in dashboard; se passa, stampa il perche'.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.clock import utcnow  # noqa: E402
from core.enums import Category, EvidenceType, SourceTier  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.schemas import DetectedEvent, Evidence  # noqa: E402
from market.prices import PublicPriceProvider  # noqa: E402
from quant.features import log_returns, stdev  # noqa: E402
from workers.runner import Runner  # noqa: E402

OPEN_NOW = ["CS.D.BITCOIN.CFD.IP", "CS.D.ETHUSD.CFD.IP", "CS.D.EURUSD.CFD.IP", "CS.D.GBPUSD.CFD.IP", "CS.D.USDJPY.CFD.IP", "CS.D.CFDGOLD.CFDGC.IP"]


async def biggest_real_move(runner: Runner):
    provider = PublicPriceProvider()
    best = None
    for epic in OPEN_NOW:
        inst = runner.registry.get(epic)
        if inst is None:
            continue
        candles = await provider.candles(inst, interval="1m", range_="1d")
        if len(candles) < 40:
            continue
        recent = candles[-60:]
        values = [c.close for c in recent]
        move = values[-1] / values[0] - 1
        sigma = stdev(log_returns([c.close for c in candles[-240:]])) or 1e-9
        zscore = (values[-1] / values[-2] - 1) / sigma if len(values) >= 2 else 0
        cand = {"epic": epic, "name": inst.name, "move_1h": move, "last": values[-1], "sigma_move": abs(move) / (sigma * (len(recent) ** 0.5)), "zscore": zscore}
        if best is None or abs(cand["move_1h"]) > abs(best["move_1h"]):
            best = cand
    await provider.aclose()
    return best


async def main() -> None:
    configure_logging("WARNING")
    runner = Runner()
    await runner.setup()
    assert runner.pipeline and runner.prices
    # forza le quote fresche degli strumenti aperti
    await runner.collectors["ig_prices"].run_once()
    mv = await biggest_real_move(runner)
    if mv is None:
        print("nessun dato di mercato disponibile ora")
        await runner.shutdown()
        return
    quote = runner.prices.cached(mv["epic"]) or await runner.prices.quote(mv["epic"])
    direction = "in rialzo" if mv["move_1h"] > 0 else "in ribasso"
    print("\n== MERCATO APERTO CON IL MOVIMENTO REALE PIU MARCATO (ultima ora):")
    print(f"   {mv['name']}  {mv['move_1h']*100:+.2f}% ({direction}), prezzo {mv['last']:.2f}, ~{mv['sigma_move']:.1f} sigma, tradeable={quote.market_status.value}")
    now = utcnow()
    ev = DetectedEvent(
        event_id=f"DEMO-{mv['epic']}-{now.strftime('%H%M%S')}", kind="ANOMALY",
        title=f"[DEMO] {mv['name']} {mv['move_1h']*100:+.2f}% nell'ultima ora ({mv['sigma_move']:.1f} sigma): momentum crypto/FX in orario di mercato aperto",
        summary=f"Movimento di prezzo REALE osservato su {mv['name']}: {mv['move_1h']*100:+.2f}% in 60 minuti (last {mv['last']:.2f}). Verifica se il comitato trova edge residuo netto dopo i costi.",
        category=Category.CRYPTO if "BITCOIN" in mv["epic"] or "ETH" in mv["epic"] else Category.MACRO,
        occurred_at=now - timedelta(minutes=2),
        evidence=[Evidence(evidence_id=f"px-{mv['epic']}", type=EvidenceType.MARKET, source="mercato (prezzi reali)", source_tier=SourceTier.TIER_2, timestamp=now - timedelta(minutes=2), reliability=0.85, impact=min(1.0, mv["sigma_move"]/4), is_confirmed=True, summary=f"{mv['name']} {mv['move_1h']*100:+.2f}%/1h reale")],
        entities=[mv["name"]], surprise=mv["move_1h"], source_reliability=0.85, is_verified=True,
        raw={"demo": True, "real_move_1h": mv["move_1h"], "zscore": mv["zscore"]},
    )
    print(f"\n== IN INGRESSO NEL COMITATO (mode={runner.settings.execution_mode.value}, autonomy={runner.settings.autonomy_level.value})...")
    outcome = await runner.pipeline.handle_event(ev)
    print(f"\n== ESITO: {outcome.stage} - {outcome.detail[:280]}  (costo LLM ${outcome.cost_usd:.3f})")
    if outcome.judge:
        j = outcome.judge
        print(f"   JUDGE: {j.decision} {j.instrument or ''} {j.direction or ''} stop={j.stop_distance_pct} target={j.target_distance_pct} risk={j.requested_risk_eur}EUR conf={j.confidence}")
        for e in (j.explanation or [])[:5]:
            print(f"     - {e}")
    if outcome.risk_decision and not outcome.risk_decision.approved:
        print("   RISK KERNEL falliti:", [c.name for c in outcome.risk_decision.failed_checks])
    from core.db import session_scope
    from core.repository import Repository
    async with session_scope(write=False) as s:
        pos = await Repository(s).open_positions(runner.settings.execution_mode.value)
    if pos:
        print("\n== POSIZIONE APERTA (visibile in dashboard):")
        for p in pos:
            print(f"   {p.instrument_name} {p.direction} size={p.size} @ {p.entry_price} stop={p.stop_level} target={p.limit_level} rischio={p.risk_eur}EUR time-stop={p.max_holding_until}")
    else:
        print("\n== Nessuna posizione aperta: il comitato non ha trovato edge sufficiente (decisione corretta se il movimento e' gia prezzato o i costi lo mangiano).")
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
