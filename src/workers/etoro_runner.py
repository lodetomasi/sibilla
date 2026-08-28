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
MAX_PAIRS_TRIED_PER_CYCLE = 5
# 65 giornate: copre sia il lookback momentum (20) sia quello pairs (60,
# strategies/etoro_pairs.py) con lo stesso fetch condiviso - il costo API e'
# identico a prescindere da count (un'unica chiamata per strumento).
CANDLE_HISTORY_DAYS = 65
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
        self._last_news_seen: dict[int, str] = {}

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
            history = await self.candles.daily_candles(instrument_id=c.instrument_id, count=CANDLE_HISTORY_DAYS)
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
            # niente return qui: i pairs sono una strategia indipendente (nessun
            # candidato momentum non deve bloccare la valutazione delle coppie).

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
            news_brief = await self.news_lookup_fn(m.name)
            # Le candele giornaliere non cambiano infra-day: senza questo dedup lo
            # stesso titolo veniva ri-giudicato dall'LLM ad ogni ciclo (~9 min con
            # 200 strumenti) per ore, sempre con lo stesso esito (visto in produzione
            # 28/8, Ackermans & Van Haaren giudicato 9+ volte in ~90 min). Un cooldown
            # a tempo pero' bloccherebbe anche un titolo con notizie APPENA arrivate
            # (visto lo stesso giorno: AECOM ha ricevuto notizia reale ma un cooldown
            # da 1h l'avrebbe tenuto bloccato) - il dedup e' sul CONTENUTO delle
            # notizie, non sul tempo: rigiudica ogni volta che cambia qualcosa.
            if news_brief == self._last_news_seen.get(m.instrument_id):
                continue
            self._last_news_seen[m.instrument_id] = news_brief
            verdict = await self.judge_fn(m, news_brief=news_brief, llm=self.llm)
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

        await self._run_pairs_phase(pairs=pairs, positions=positions, account=account, context=context)

    async def _run_pairs_phase(self, *, pairs, positions, account, context) -> None:
        """Mean-reversion market-neutral, indipendente dalla strategia momentum:
        nessuna notizia, nessun LLM - solo correlazione storica + z-score dello
        spread. Una coppia = due gambe (long+short) aperte insieme o per niente
        (all-or-nothing): il motore di rischio condiviso valuta ogni gamba come
        un trade a se', ciascuna con meta' del budget di rischio standard.
        """
        from strategies.etoro_pairs import find_pair_signals
        from risk.etoro_pairs_adapter import (
            LEVERAGE as PAIRS_LEVERAGE,
            RISK_FRACTION_PER_LEG,
            build_leg_proposal,
            leg_entry_price,
            leg_stop_and_target,
            size_from_decision as pair_size_from_decision,
        )

        if len(positions) + 2 > MAX_OPEN_POSITIONS:
            log.info("etoro.runner.pairs_skipped_position_cap", open=len(positions))
            return

        pair_signals = find_pair_signals(pairs)
        for sig in pair_signals:
            log.info(
                "etoro.pairs.evaluated", instrument_a_id=sig.instrument_a_id, instrument_a_name=sig.instrument_a_name,
                instrument_b_id=sig.instrument_b_id, instrument_b_name=sig.instrument_b_name,
                correlation=round(sig.correlation, 3), z_score=round(sig.z_score, 2),
            )

        for sig in pair_signals[:MAX_PAIRS_TRIED_PER_CYCLE]:
            leg_quotes = await self.rates.quotes_for([sig.instrument_a_id, sig.instrument_b_id])
            leg_quote_by_id = {instrument_id_from_epic(q.epic): q for q in leg_quotes}
            quote_a = leg_quote_by_id.get(sig.instrument_a_id)
            quote_b = leg_quote_by_id.get(sig.instrument_b_id)
            if quote_a is None or quote_b is None:
                continue

            pair_label = f"pair-{sig.instrument_a_id}-{sig.instrument_b_id}"
            risk_per_leg = account.equity * RISK_FRACTION_PER_LEG
            proposal_a = build_leg_proposal(instrument_id=sig.instrument_a_id, name=sig.instrument_a_name, direction=sig.direction_a, quote=quote_a, pair_label=pair_label, requested_risk_eur=risk_per_leg)
            proposal_b = build_leg_proposal(instrument_id=sig.instrument_b_id, name=sig.instrument_b_name, direction=sig.direction_b, quote=quote_b, pair_label=pair_label, requested_risk_eur=risk_per_leg)
            decision_a = self.risk_engine.evaluate(proposal_a, context, fx_rate_to_eur=1.0)
            decision_b = self.risk_engine.evaluate(proposal_b, context, fx_rate_to_eur=1.0)
            if not (decision_a.approved and decision_b.approved):
                log.info(
                    "etoro.runner.pair_risk_rejected", pair=pair_label,
                    reasons_a=decision_a.rejection_reasons, reasons_b=decision_b.rejection_reasons,
                )
                continue

            units_a, units_b = pair_size_from_decision(decision_a), pair_size_from_decision(decision_b)
            if units_a <= 0 or units_b <= 0:
                continue

            entry_a, entry_b = leg_entry_price(sig.direction_a, quote_a), leg_entry_price(sig.direction_b, quote_b)
            stop_a, target_a = leg_stop_and_target(sig.direction_a, entry_a)
            stop_b, target_b = leg_stop_and_target(sig.direction_b, entry_b)
            await self.gateway.open_market_order(instrument_id=sig.instrument_a_id, direction=sig.direction_a, units=units_a, stop_loss=stop_a, take_profit=target_a, leverage=PAIRS_LEVERAGE)
            await self.gateway.open_market_order(instrument_id=sig.instrument_b_id, direction=sig.direction_b, units=units_b, stop_loss=stop_b, take_profit=target_b, leverage=PAIRS_LEVERAGE)
            # una sola coppia aperta per ciclo: la capacita' posizioni (MAX_OPEN_POSITIONS)
            # e' condivisa con la strategia momentum, non si accumula piu' di una coppia alla volta.
            break

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
