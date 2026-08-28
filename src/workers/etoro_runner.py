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
MAX_JUDGED_PER_CYCLE = 5
MAX_OPEN_POSITIONS = 3  # RiskLimits.max_open_positions (default 10) non e' overridabile
# da env flat in questo repo: il cap di design (max 3) e' applicato qui, non nel RiskEngine.

log = get_logger("workers.etoro_runner")

JudgeFn = Callable[..., Awaitable[CatalystVerdict]]


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
        risk_engine: RiskEngine | None = None,
    ):
        self.universe = universe
        self.rates = rates
        self.candles = candles
        self.gateway = gateway
        self.llm = llm
        self.judge_fn = judge_fn
        self.risk_engine = risk_engine or RiskEngine()

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
        from strategies.etoro_momentum import momentum_candidates

        pairs = []
        for c in candidates:
            history = await self.candles.daily_candles(instrument_id=c.instrument_id, count=21)
            pairs.append((c, history))
        momentum = momentum_candidates(pairs)[:MAX_JUDGED_PER_CYCLE]
        if not momentum:
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

        for m in momentum:
            quote = quote_by_id.get(m.instrument_id)
            if quote is None:
                continue
            verdict = await self.judge_fn(m, news_brief="", llm=self.llm)
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

    from collectors.etoro.instruments import InstrumentUniverse
    from collectors.etoro.rates import CandleHistory, RatesCollector
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
    await client.aclose()
    log.info("etoro.runner.stopped")


if __name__ == "__main__":
    asyncio.run(main())
