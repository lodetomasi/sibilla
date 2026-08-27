"""Simulazione end-to-end (sez. 54-56, patch sez. 34-35).

1) BACKTEST deterministico su prezzi REALI (fallback pubblico, 1 minuto, ultimi 5 giorni):
   eventi sintetizzati dai salti di prezzo osservati sul leader (proxy di catalyst)
   -> cross-asset lag / repricing -> stop/target/time-stop -> metriche, R-multipli, walk-forward.
   Dice se la MECCANICA (costi, stop, R:R, latenza) puo produrre alpha con edge ipotizzato.
2) LIVE PAPER RUN (opzionale, --live-minutes N): lancia il runner in PAPER con il
   comitato LLM reale su eventi reali e riporta il journal.

Uso:  .venv/bin/python scripts/simulate.py [--days 5] [--live-minutes 0]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.clock import utcnow  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from evaluation.backtest import simulate_event_trades, walk_forward  # noqa: E402
from market.instrument_registry import get_registry  # noqa: E402
from market.prices import PublicPriceProvider  # noqa: E402
from quant.features import log_returns, stdev  # noqa: E402

LEADERS = {"IR.D.10YEAR100.FWM2.IP": ["IX.D.NASDAQ.IFE.IP", "CS.D.CFDGOLD.CFDGC.IP", "CS.D.DOLLARINDEX.CFD.IP"],
           "CS.D.DOLLARINDEX.CFD.IP": ["CS.D.EURUSD.CFD.IP", "CS.D.CFDGOLD.CFDGC.IP"],
           "IX.D.SPTRD.IFE.IP": ["IX.D.NASDAQ.IFE.IP", "IX.D.DOW.IFE.IP", "IX.D.RUSSELL.IFE.IP"],
           "CC.D.CL.UNC.IP": ["CC.D.LCO.UNC.IP"],
           "CS.D.BITCOIN.CFD.IP": ["CS.D.ETHUSD.CFD.IP"]}


async def load_series(days: int) -> dict[str, list]:
    provider = PublicPriceProvider()
    registry = get_registry()
    series: dict[str, list] = {}
    for inst in registry.all():
        candles = await provider.candles(inst, interval="1m", range_=f"{days}d")
        if candles:
            series[inst.epic] = [(c.ts, c.close) for c in candles]
        print(f"  {inst.name:22s} {len(candles):5d} candele 1m ({inst.fallback_symbol})")
    await provider.aclose()
    return series


def synth_events(series: dict[str, list], *, sigma_threshold: float = 3.0, window: int = 5) -> list[dict]:
    """Catalyst proxy: salto del leader > k sigma in `window` minuti -> evento per i follower correlati."""
    registry = get_registry()
    events: list[dict] = []
    for leader, followers in LEADERS.items():
        s = series.get(leader)
        if not s or len(s) < 200:
            continue
        values = [v for _, v in s]
        rets = log_returns(values)
        base_sigma = stdev(rets[:300]) or stdev(rets)
        if not base_sigma:
            continue
        last_event_idx = -10**9
        for i in range(window, len(values)):
            move = values[i] / values[i - window] - 1
            if abs(move) > sigma_threshold * base_sigma * (window ** 0.5) and i - last_event_idx > 60:
                last_event_idx = i
                ts = s[i][0]
                for follower in followers:
                    if follower not in series:
                        continue
                    corr = next((score for other, score in registry.related(leader, min_overlap=0.2) if other.epic == follower), 0.5)
                    direction = "BUY" if move * corr > 0 else "SELL"
                    expected = min(0.01, abs(move) * abs(corr) * 0.8)
                    events.append({"ts": ts, "epic": follower, "direction": direction, "expected_move_pct": expected, "leader": leader, "leader_move": move})
    events.sort(key=lambda e: e["ts"])
    return events


async def backtest(days: int) -> dict:
    registry = get_registry()
    print(f"\n== Scarico prezzi reali (1m, {days}d) ==")
    series = await load_series(days)
    events = synth_events(series)
    print(f"\n== Eventi sintetizzati da salti del leader: {len(events)} ==")
    instruments = {i.epic: i for i in registry.all()}
    settings = get_settings()
    equity = settings.risk.bankroll
    result = simulate_event_trades(events=events, series_by_epic=series, instruments=instruments, equity=equity, risk_per_trade=settings.risk.max_risk_per_trade, min_net_alpha_pct=settings.risk.min_net_alpha, reward_risk=settings.risk.min_reward_risk)
    metrics = result.metrics()
    print("\n== Backtest meccanico (nessun LLM, edge = shock leader x correlazione strutturale) ==")
    print(json.dumps(metrics, indent=1, default=str))
    reasons: dict[str, int] = {}
    for t in result.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    print("uscite:", reasons)
    wf = walk_forward(events, train_days=2, test_days=1, run=lambda train, test: simulate_event_trades(events=test, series_by_epic=series, instruments=instruments, equity=equity).metrics())
    print("\n== Walk-forward (train 2d / test 1d) ==")
    for w in wf:
        m = w["metrics"]
        print(f"  test {w['test'][0][:10]} -> n={m.get('n')} pnl={m.get('pnl')} win={m.get('win_rate')} mean_r={m.get('mean_r')}")
    return {"metrics": metrics, "exits": reasons, "walk_forward": wf, "n_events": len(events)}


async def live_paper(minutes: int) -> dict:
    from workers.runner import Runner

    runner = Runner()
    await runner.setup()
    assert runner.engine and runner.monitor and runner.pipeline
    from collectors.base import CollectionMode

    print(f"\n== LIVE PAPER RUN {minutes} min (mode={runner.settings.execution_mode.value}, autonomy={runner.settings.autonomy_level.value}) ==")
    await runner.collectors["ig_prices"].run_once(CollectionMode.HISTORICAL_BATCH, minutes=24 * 60)
    deadline = utcnow() + timedelta(minutes=minutes)
    tasks = [asyncio.create_task(runner._loop("ig_prices", 15.0, runner.collectors["ig_prices"].run_once)),
             asyncio.create_task(runner._loop("news_rss", 45.0, runner.collectors["news_rss"].run_once)),
             asyncio.create_task(runner._loop("polymarket_markets", 60.0, runner.collectors["polymarket_markets"].run_once, CollectionMode.INCREMENTAL, limit=80)),
             asyncio.create_task(runner._loop("polymarket_scan", 90.0, runner._scan_polymarket)),
             asyncio.create_task(runner._loop("monitor", 10.0, runner.monitor.tick))]
    while utcnow() < deadline:
        await asyncio.sleep(30)
        print(f"  [{utcnow().strftime('%H:%M:%S')}] pipeline outcomes: {len(runner.pipeline.history)} | pending tasks: {sum(1 for t in runner.tasks if not t.done())} | loops: {{k: v.get('result', v.get('error')) for k, v in runner.status.items()}}")
    pending = [t for t in runner.tasks if not t.done()]
    if pending:
        print(f"  attendo {len(pending)} decisioni in corso (max 10 min)...")
        await asyncio.wait(pending, timeout=600)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    outcomes = [o.as_dict() for o in runner.pipeline.history]
    from evaluation.pnl import realized_performance

    perf = await realized_performance(mode=runner.settings.execution_mode.value)
    llm_cost = runner.pipeline.llm.budget.spent_today
    await runner.shutdown()
    return {"outcomes": outcomes, "performance": perf, "llm_cost_usd_today": llm_cost}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--live-minutes", type=int, default=0)
    args = parser.parse_args()
    configure_logging("WARNING")
    report: dict = {"generated_at": utcnow().isoformat()}
    report["backtest"] = await backtest(args.days)
    if args.live_minutes > 0:
        report["live_paper"] = await live_paper(args.live_minutes)
        print("\n== LIVE PAPER: esiti pipeline ==")
        for o in report["live_paper"]["outcomes"]:
            print(f"  {o['stage']:18s} {o.get('judge') or '':12s} {str(o.get('epic') or ''):24s} ${o['cost_usd']:.3f}  {o['title'][:70]}")
        print("performance:", json.dumps(report["live_paper"]["performance"], default=str)[:600])
        print("costo LLM oggi: $", round(report["live_paper"]["llm_cost_usd_today"], 3))
    out = Path("data") / f"simulation_{utcnow().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nreport salvato in {out}")


if __name__ == "__main__":
    asyncio.run(main())
