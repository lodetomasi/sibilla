"""Runner eToro: universo -> screener -> judge -> risk -> gateway.

Orario di mercato SEMPRE calcolato con zoneinfo("America/New_York"): mai un
range UTC hardcoded (la differenza EST/EDT sposta la finestra di 1h, e un
range fisso sbaglierebbe per 8 mesi/anno durante l'ora legale USA).
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, time as dtime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from core.bus import emit
from core.clock import utcnow
from core.config import get_settings
from core.enums import Direction
from core.logging import configure_logging, get_logger
from collectors.etoro.news_lookup import recent_news_brief
from execution.etoro.client import EtoroClient
from execution.etoro.gateway import EtoroGateway, instrument_id_from_epic
from intelligence.etoro_judge import CatalystVerdict, judge_catalyst
from risk.engine import PortfolioContext, RiskEngine
from risk.etoro_adapter import LEVERAGE, RISK_FRACTION_OF_EQUITY, build_trade_proposal, size_from_decision
from risk.correlation import OpenExposure
from risk.kill_switch import get_kill_switch

NY = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
TIME_STOP = dtime(15, 40)
SCAN_INTERVAL_S = 300
MAX_JUDGED_PER_CYCLE = 10
JUDGE_COOLDOWN_S = 3600  # stesso pattern del vecchio motore Limitless: le candele
# giornaliere non cambiano infra-day, senza cooldown lo stesso titolo verrebbe
# ri-giudicato dall'LLM ad ogni ciclo (~9 min con 200 strumenti) per ore, sempre
# con lo stesso esito - visto in produzione 28/8 (Ackermans & Van Haaren, 9+ volte).
MAX_OPEN_POSITIONS = 3  # RiskLimits.max_open_positions (default 10) non e' overridabile
# da env flat in questo repo: il cap di design (max 3) e' applicato qui, non nel RiskEngine.

log = get_logger("workers.etoro_runner")

JudgeFn = Callable[..., Awaitable[CatalystVerdict]]
NewsLookupFn = Callable[..., Awaitable[str]]


class EtoroRunner:
    def __init__(
        self,
        *,
        universe: Any,
        rates: Any,
        candles: Any,
        gateway: Any,
        llm: Any,
        judge_fn: JudgeFn = judge_catalyst,
        news_lookup_fn: NewsLookupFn = recent_news_brief,
        risk_engine: RiskEngine | None = None,
    ):
        self.universe = universe
        self.rates = rates
        self.candles = candles
        self.gateway = gateway
        self.llm = llm
        self.judge_fn = judge_fn
        self.news_lookup_fn = news_lookup_fn
        self.risk_engine = risk_engine or RiskEngine()
        self._last_judged_at: dict[int, datetime] = {}

    def is_market_open(self, now: datetime) -> bool:
        local = now.astimezone(NY)
        if local.weekday() >= 5:
            return False
        return MARKET_OPEN <= local.time() < MARKET_CLOSE

    def is_time_stop(self, now: datetime) -> bool:
        return now.astimezone(NY).time() >= TIME_STOP

    async def run_cycle(self) -> None:
        try:
            self.risk_engine.kill_switch.guard()
        except Exception as exc:  # noqa: BLE001
            log.warning("etoro.runner.kill_switch_active", error=str(exc)[:160])
            return

        positions = await self.gateway.positions()
        if len(positions) >= MAX_OPEN_POSITIONS:
            log.info("etoro.runner.position_cap_reached", open=len(positions))
            return

        candidates = await self.universe.refresh()
        from strategies.etoro_momentum import MomentumCandidate, evaluate_momentum

        pairs = []
        for c in candidates:
            history = await self.candles.daily_candles(instrument_id=c.instrument_id, count=21)
            pairs.append((c, history))
        evaluations = evaluate_momentum(pairs)
        for e in evaluations:
            log.info(
                "etoro.momentum.evaluated", instrument_id=e.instrument_id, name=e.name,
                gap_pct=round(e.gap_pct, 4), relative_volume=round(e.relative_volume, 2), qualifies=e.qualifies,
            )
        momentum = [
            MomentumCandidate(instrument_id=e.instrument_id, name=e.name, price=e.price, gap_pct=e.gap_pct, relative_volume=e.relative_volume)
            for e in evaluations
            if e.qualifies
        ][:MAX_JUDGED_PER_CYCLE]
        if not momentum:
            log.info("etoro.runner.no_momentum_candidates", scanned=len(candidates), evaluated=len(evaluations))
            return

        quotes = await self.rates.quotes_for([m.instrument_id for m in momentum])
        quote_by_id = {instrument_id_from_epic(q.epic): q for q in quotes}

        account = await self.gateway.balances()
        open_exposures = [
            OpenExposure(
                epic=p.epic, direction=p.direction, notional=p.size * p.level,
                risk_eur=(abs(p.level - p.stop_level) * p.size) if p.stop_level is not None else 0.0,
                asset_class="EQUITY_CFD", currency=p.currency,
            )
            for p in positions
        ]
        context = PortfolioContext(
            account=account, open_positions=open_exposures, realized_pnl_today=0.0, realized_pnl_week=0.0,
            peak_equity_week=account.equity, trades_today=0, rejected_streak=0,
        )

        now = utcnow()
        for m in momentum:
            last_judged = self._last_judged_at.get(m.instrument_id)
            if last_judged is not None and (now - last_judged).total_seconds() < JUDGE_COOLDOWN_S:
                continue
            quote = quote_by_id.get(m.instrument_id)
            if quote is None:
                continue
            news_brief = await self.news_lookup_fn(m.name)
            verdict = await self.judge_fn(m, news_brief=news_brief, llm=self.llm)
            self._last_judged_at[m.instrument_id] = now
            if not verdict.has_catalyst:
                log.info("etoro.runner.no_catalyst", instrument_id=m.instrument_id)
                continue
            requested_risk = account.equity * RISK_FRACTION_OF_EQUITY
            proposal = build_trade_proposal(m, verdict, quote, event_id=f"etoro-{m.instrument_id}", requested_risk_eur=requested_risk)
            decision = self.risk_engine.evaluate(proposal, context, fx_rate_to_eur=1.0)
            if not decision.approved:
                log.info("etoro.runner.risk_rejected", instrument_id=m.instrument_id, reasons=decision.rejection_reasons)
                continue
            units = size_from_decision(decision)
            if units <= 0:
                continue
            await self.gateway.open_market_order(
                instrument_id=m.instrument_id, direction=Direction.BUY, units=units,
                stop_loss=round(quote.offer * (1 - 0.07), 2), take_profit=round(quote.offer * (1 + 0.14), 2),
                leverage=LEVERAGE,
            )

    async def time_stop_close_all(self) -> None:
        positions = await self.gateway.positions()
        for p in positions:
            await self.gateway.close_position(
                position_id=p.deal_id, instrument_id=instrument_id_from_epic(p.epic), units=p.size
            )


async def main() -> None:
    configure_logging()
    settings = get_settings()
    client = EtoroClient(settings=settings)
    gateway = EtoroGateway(client=client, emit=emit)

    from collectors.base import CollectionMode
    from collectors.etoro.instruments import InstrumentUniverse
    from collectors.etoro.rates import CandleHistory, RatesCollector
    from collectors.news.rss import RSSNewsCollector
    from core.config import DATA_DIR
    from intelligence.llm import get_llm_client

    universe = InstrumentUniverse(client=client, cache_path=DATA_DIR / "etoro_universe.json", max_price_usd=settings.etoro.max_penny_price_usd)
    rates = RatesCollector(client=client)
    candles = CandleHistory(client=client)
    llm = get_llm_client()
    runner = EtoroRunner(universe=universe, rates=rates, candles=candles, gateway=gateway, llm=llm)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    news_collector = RSSNewsCollector()

    async def _news_loop() -> None:
        # Popola il DB news usato da recent_news_brief (il judge anti pump&dump ha
        # bisogno di notizie reali, non di un contesto sempre vuoto). Cadenza
        # indipendente dal ciclo di scan/trade: le notizie contano anche fuori
        # dall'orario di mercato per il ciclo successivo all'apertura.
        while not stop.is_set():
            try:
                await news_collector.collect(mode=CollectionMode.INCREMENTAL)
            except Exception:  # noqa: BLE001
                log.exception("etoro.runner.news_collect_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=900)
            except TimeoutError:
                pass

    news_task = asyncio.create_task(_news_loop())

    log.info("etoro.runner.started")
    already_time_stopped_today: str | None = None
    while not stop.is_set():
        now = utcnow()
        today = now.date().isoformat()
        if runner.is_time_stop(now) and already_time_stopped_today != today:
            await runner.time_stop_close_all()
            already_time_stopped_today = today
        elif runner.is_market_open(now):
            try:
                await runner.run_cycle()
            except Exception:  # noqa: BLE001
                log.exception("etoro.runner.cycle_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_S)
        except TimeoutError:
            pass
    news_task.cancel()
    await news_collector.aclose()
    await client.aclose()
    log.info("etoro.runner.stopped")


if __name__ == "__main__":
    asyncio.run(main())
