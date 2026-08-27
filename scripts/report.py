"""Report unico: 'stiamo facendo soldi? c'e' edge?' — legge tutto dal DB, nessun costo LLM.

Mostra: posizioni aperte + P&L, performance realizzata, post-signal alpha (informazione
anticipatoria reale), reliability per modello/categoria, calibrazione, costi, funnel decisioni.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.clock import utcnow  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.db import session_scope  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.repository import Repository  # noqa: E402


def _fmt(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


async def main() -> None:
    configure_logging("ERROR")
    settings = get_settings()
    mode = settings.execution_mode.value
    print(f"\n{'='*70}\n ATS REPORT — {utcnow():%Y-%m-%d %H:%M UTC} — mode {mode}\n{'='*70}")

    from evaluation.alpha import post_signal_alpha_report
    from evaluation.attribution import attribution_report
    from evaluation.metrics import performance_by_domain
    from evaluation.pnl import execution_quality, realized_performance

    async with session_scope(write=False) as s:
        repo = Repository(s)
        snap = await repo.latest_portfolio(mode)
        positions = list(await repo.open_positions(mode))
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        pnl_today = await repo.realized_pnl_since(today, mode)
        pnl_total = await repo.realized_pnl_since(utcnow() - timedelta(days=3650), mode)
        list(await repo.journal_entries(mode=mode, limit=5000))
        events_by_stage: dict[str, int] = {}
        for e in await repo.recent_detected_events(minutes=7*24*60, limit=5000):
            events_by_stage[e.status] = events_by_stage.get(e.status, 0) + 1
        costs = await repo.costs_since(utcnow() - timedelta(days=30))

    equity = float(snap.equity) if snap else settings.risk.bankroll
    print(f"\n[CONTO]  equity €{_fmt(equity)}  |  P&L oggi €{_fmt(pnl_today)}  |  P&L totale €{_fmt(pnl_total)}  |  posizioni aperte {len(positions)}")
    for p in positions:
        upnl = float(p.unrealized_pnl or 0)
        print(f"   • {p.instrument_name} {p.direction} size={p.size} @ {_fmt(p.entry_price)}  ora {_fmt(p.current_price)}  P&L €{_fmt(upnl)}  stop {_fmt(p.stop_level)} target {_fmt(p.limit_level)}")

    perf = await realized_performance(mode=mode)
    print(f"\n[PERFORMANCE REALIZZATA]  trade chiusi {perf.get('n',0)}  |  P&L €{_fmt(perf.get('pnl'))}  |  win {_fmt(perf.get('win_rate'),3)}  |  expectancy €{_fmt(perf.get('expectancy'),3)}  |  profit factor {_fmt(perf.get('profit_factor'),2)}  |  Sharpe {_fmt(perf.get('sharpe'),2)}  |  maxDD €{_fmt(perf.get('max_drawdown'))}")
    exe = await execution_quality(mode=mode)
    print(f"[EXECUTION]  ordini {exe.get('orders',0)}  fill_rate {_fmt(exe.get('fill_rate'),2)}  slippage medio {_fmt(exe.get('avg_slippage_pct'),5)}  latenza p95 {_fmt(exe.get('p95_latency_ms'),0)}ms")

    psa = await post_signal_alpha_report(mode=mode)
    print("\n[POST-SIGNAL ALPHA]  (return medio dopo il segnale nel verso del trade — >0 e crescente = informazione anticipatoria reale)")
    for h in ("30s","1m","5m","15m","1h"):
        st = psa["overall"].get(h, {})
        if st.get("n"):
            print(f"   {h:>4}: media {_fmt(st.get('mean'),5)}  hit {_fmt(st.get('hit_rate'),2)}  n={st['n']}")
    if psa.get("n_trades"):
        print(f"   trade valutati: {psa['n_trades']}")

    rel = await performance_by_domain()
    if rel:
        print("\n[RELIABILITY PER MODELLO x CATEGORIA]  (hit-rate / brier / n — chi ha davvero ragione, appreso dai dati)")
        for scope, cats in rel.items():
            allc = cats.get("ALL") or next(iter(cats.values()), {})
            print(f"   {scope:24s} ALL: hit {_fmt(allc.get('hit_rate'),2)} brier {_fmt(allc.get('brier'),3)} n={allc.get('n')}")

    attr = await attribution_report(mode=mode)
    if attr.get("by_source"):
        print(f"\n[ALPHA ATTRIBUTION]  P&L €{_fmt(attr['total_pnl'])} -> " + "  ".join(f"{k}: €{_fmt(v)}" for k,v in attr["by_source"].items()))

    print("\n[FUNNEL DECISIONI]  " + "  ".join(f"{k}: {v}" for k,v in sorted(events_by_stage.items(), key=lambda x:-x[1])))
    print(f"[COSTO LLM 30gg]  ${_fmt(sum(costs.values()),3)}  ({', '.join(f'{k} ${_fmt(v,3)}' for k,v in costs.items()) or 'n/a'})")

    # verdetto onesto
    n = perf.get("n", 0)
    print("\n[VERDETTO]")
    if n < 30:
        print(f"   Campione insufficiente ({n} trade chiusi). Servono >=100 trade out-of-sample per dire se c'e' edge.")
    elif (perf.get('expectancy') or 0) > 0 and (perf.get('profit_factor') or 0) > 1.1:
        print(f"   Segnali positivi: expectancy €{_fmt(perf.get('expectancy'),3)}, profit factor {_fmt(perf.get('profit_factor'),2)} su {n} trade. Continuare a validare.")
    else:
        print(f"   Nessun edge dimostrato finora su {n} trade (expectancy {_fmt(perf.get('expectancy'),3)}). NON passare a capitale reale.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
