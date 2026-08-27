"""Dimostrazione del PERCORSO D'ORDINE (risk kernel + execution PAPER + monitor) su
mercato aperto e prezzo REALE. Etichettata come demo del meccanismo: bypassa il
giudizio 'e' un catalizzatore?' del comitato (che ora dice giustamente no), ma NON
bypassa nulla del rischio: size dal rischio, stop obbligatorio, R:R, margine, ecc.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.clock import utcnow  # noqa: E402
from core.enums import Direction, EntryType, ReasonCode, SignalType  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.pricing import worse_price  # noqa: E402
from core.schemas import CostEstimate, ResidualAlpha, TradeProposal  # noqa: E402
from workers.runner import Runner  # noqa: E402

EPIC = "CS.D.ETHUSD.CFD.IP"  # crypto: aperto 24/7


async def main() -> None:
    configure_logging("WARNING")
    runner = Runner()
    await runner.setup()
    assert runner.engine and runner.prices
    await runner.collectors["ig_prices"].run_once()
    inst = runner.registry.get(EPIC)
    quote = runner.prices.cached(EPIC) or await runner.prices.quote(EPIC)
    entry = quote.price_for(Direction.BUY)
    stop_pct, target_pct = 0.004, 0.008  # stop 0.4%, target 0.8% -> R:R 2.0
    print(f"\n== {inst.name}: prezzo reale bid={quote.bid} offer={quote.offer} fonte={quote.source} status={quote.market_status.value}")
    proposal = TradeProposal(
        trade_id=f"DEMO{utcnow().strftime('%H%M%S')}{uuid.uuid4().hex[:4].upper()}", event_id="DEMO-EXEC", strategy_id="A_BREAKING_NEWS",
        signal_type=SignalType.BREAKING_NEWS_REPRICING, instrument=inst, epic=EPIC, direction=Direction.BUY, entry_type=EntryType.MARKET,
        quote=quote, max_entry=worse_price(entry, 0.0005, "BUY"), stop_distance=entry * stop_pct, limit_distance=entry * target_pct,
        time_horizon_seconds=1800, expected_return_pct=0.008, expected_loss_pct=stop_pct, probability=0.6, confidence=0.65,
        requested_risk_eur=5.0, reason_code=ReasonCode.NEWS_NOT_FULLY_PRICED,
        residual=ResidualAlpha(epic=EPIC, direction=Direction.BUY, expected_move_pct=0.008, realized_move_pct=0.001, residual_move_pct=0.007, costs=CostEstimate(spread_pct=quote.spread_pct), net_alpha_pct=0.006, passes=True),
        invalidation_conditions=["prezzo torna sotto il livello di ingresso", "time stop 30 min"],
        explanation=["DEMO percorso d'ordine su prezzo reale", f"{inst.name} entry {entry:.2f}", "stop 0.4% / target 0.8% (R:R 2.0)", "size calcolata dal rischio (5 EUR)", "esecuzione PAPER su feed reale"],
    )
    decision = await runner.engine.assess(proposal)
    print(f"\n== RISK KERNEL: approved={decision.approved} size={decision.size} rischio={decision.risk_eur:.2f}EUR stop={decision.stop_level:.2f} target={decision.limit_level:.2f} notional={decision.notional:.0f} margine={decision.margin_required:.0f} leva={decision.effective_leverage:.2f}x")
    if not decision.approved:
        print("   falliti:", [(c.name, c.detail[:80]) for c in decision.failed_checks])
        await runner.shutdown()
        return
    result = await runner.engine.submit(proposal, decision, quote=quote)
    print(f"\n== ESECUZIONE PAPER: status={result.status} fill={result.fill_price} size={result.filled_size} slippage={result.slippage_pct}")
    from core.db import session_scope
    from core.repository import Repository
    async with session_scope(write=False) as s:
        p = await Repository(s).get_position(proposal.trade_id)
    if p:
        print(f"\n== POSIZIONE APERTA (visibile in dashboard):\n   {p.instrument_name} {p.direction} size={p.size} @ {p.entry_price:.2f}  stop={p.stop_level:.2f}  target={p.limit_level:.2f}  rischio={p.risk_eur:.2f}EUR  time-stop={p.max_holding_until}")
        print("   Il monitor la gestira' su prezzi reali: chiude a stop/target o dopo 30 min.")
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
