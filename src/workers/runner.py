"""Worker runner (sez. 46): collector, event detector, pipeline, monitor, evaluation, API.

Un processo asyncio con task separati per responsabilita; il bus in-memory li
collega (Redis Streams se disponibile). `python -m workers.runner`.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from collectors.base import CollectionMode
from collectors.ig.prices import IGPriceCollector
from collectors.macro.calendar import MacroCalendarCollector
from collectors.news.rss import RSSNewsCollector
from collectors.polymarket.markets import PolymarketMarketCollector
from collectors.polymarket.wallets import PolymarketWalletCollector
from core.bus import BusEvent, get_bus
from core.clock import utcnow
from core.config import Settings, get_settings
from core.db import create_all, session_scope
from core.enums import EventType, KillSwitchReason
from core.logging import configure_logging, get_logger
from core.repository import Repository
from core.schemas import DetectedEvent
from execution.engine import ExecutionEngine
from execution.monitor import PositionMonitor
from intelligence.event_detector import EventDetector
from intelligence.llm import get_llm_client
from intelligence.pipeline import DecisionPipeline
from intelligence.reliability import resolve_pending_predictions
from market.instrument_registry import get_registry
from market.prices import IGPriceProvider, PriceService, set_price_service
from risk.kill_switch import get_kill_switch
from strategies.catalog import ensure_registry

log = get_logger("workers.runner")


class Runner:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.tasks: list[asyncio.Task[Any]] = []
        self.registry = get_registry()
        self.kill_switch = get_kill_switch()
        self.ig_client: Any = None
        self.ig_gateway: Any = None
        self.stream: Any = None
        self.prices: PriceService | None = None
        self.engine: ExecutionEngine | None = None
        self.pipeline: DecisionPipeline | None = None
        self.monitor: PositionMonitor | None = None
        self.detector = EventDetector()
        self.collectors: dict[str, Any] = {}
        self.started_at = utcnow()
        self.status: dict[str, Any] = {}
        # max eventi elaborati in parallelo dal comitato: limita concorrenza DB e spesa LLM per burst
        self._pipeline_slots = asyncio.Semaphore(self.settings.max_concurrent_events)

    # --------------------------------------------------------------- setup
    async def setup(self) -> None:
        configure_logging(self.settings.log_level, self.settings.log_json)
        await create_all()
        await ensure_registry()
        await self.kill_switch.load()
        await self.registry.load_from_db()

        ig_provider = None
        env = self.settings.ig_environment
        if self.settings.ig_enabled and self.settings.ig.configured(env):
            from execution.ig.client import IGClient
            from execution.ig.orders import IGOrderGateway

            self.ig_client = IGClient(env)
            try:
                session = await self.ig_client.authenticate()
                # sync solo degli strumenti obsoleti: rispetta l'allowance IG del conto demo
                async with session_scope(write=False) as db:
                    rows = await Repository(db).list_instruments(active_only=False)
                    db_synced = {r.epic: r.last_synced_at for r in rows}
                report = await self.registry.sync_from_ig(self.ig_client, db_synced=db_synced)
                log.info("ig.ready", environment=env.value, account=session.account_id, updated=len(report["updated"]), replaced=report["replaced"], missing=report["missing"], skipped=len(report["skipped"]), allowance_hit=report["allowance_hit"])
                ig_provider = IGPriceProvider(self.ig_client)
                self.ig_gateway = IGOrderGateway(self.ig_client, confirm_attempts=self.settings.ig.confirm_poll_attempts, confirm_interval_s=self.settings.ig.confirm_poll_interval_s)
            except Exception as exc:  # noqa: BLE001
                log.error("ig.unavailable", error=str(exc)[:200])
                if self.settings.execution_mode.sends_orders_to_broker:
                    await self.kill_switch.trigger(KillSwitchReason.API_UNAVAILABLE, by="runner", error=str(exc)[:200])
                self.ig_client = None
        else:
            log.warning("ig.not_configured", environment=env.value, note="prezzi da fonte pubblica, execution PAPER/SHADOW")
            if self.settings.execution_mode.sends_orders_to_broker:
                raise RuntimeError(f"execution_mode {self.settings.execution_mode.value} richiede credenziali IG {env.value}")

        self.prices = PriceService(registry=self.registry, ig_provider=ig_provider, allow_public_fallback=not self.settings.execution_mode.uses_real_money)
        set_price_service(self.prices)
        await self.registry.save_to_db()
        self.engine = ExecutionEngine(settings=self.settings, registry=self.registry, prices=self.prices, ig_gateway=self.ig_gateway, ig_client=self.ig_client, kill_switch=self.kill_switch)
        if not self.settings.execution_mode.sends_orders_to_broker:
            async with session_scope(write=False) as db:
                open_rows = list(await Repository(db).open_positions(self.settings.execution_mode.value))
                reloaded = self.engine.paper.load_open_positions(open_rows)  # attributi letti dentro la sessione
            if reloaded:
                log.info("paper.positions_reloaded", count=reloaded)
        self.pipeline = DecisionPipeline(engine=self.engine, llm=get_llm_client(), prices=self.prices, registry=self.registry, settings=self.settings)
        self.monitor = PositionMonitor(self.engine)
        self.collectors = {
            "polymarket_markets": PolymarketMarketCollector(),
            "polymarket_wallets": PolymarketWalletCollector(),
            "news_rss": RSSNewsCollector(),
            "macro_calendar": MacroCalendarCollector(),
            "ig_prices": IGPriceCollector(prices=self.prices, registry=self.registry, stream_getter=lambda: self.stream),
        }
        if self.settings.limitless.enabled:
            from collectors.limitless.markets import LimitlessMarketCollector
            from intelligence.limitless_pipeline import LimitlessDecisionLoop

            collector = LimitlessMarketCollector(prices=self.prices, registry=self.registry, settings=self.settings)
            self.collectors["limitless_markets"] = collector
            self.limitless_loop = LimitlessDecisionLoop(engine=self.engine, llm=get_llm_client(), prices=self.prices, registry=self.registry, settings=self.settings, collector=collector)
        else:
            self.limitless_loop = None
        bus = await get_bus(self.settings.redis_url)
        bus.subscribe(EventType.NEWS_DETECTED, self._on_news)
        bus.subscribe(EventType.MACRO_RELEASE, self._on_macro)
        bus.subscribe(EventType.ANOMALY_DETECTED, self._on_anomaly)
        bus.subscribe(EventType.EVENT_DETECTED, self._on_event_detected)
        from alerts.notifier import Notifier

        Notifier().attach(bus)
        if self.ig_client is not None and self.settings.ig.streaming_enabled:
            await self._start_stream()

    def _streamable_epics(self) -> list[str]:
        """Solo epic validati da IG (apply_ig_details ha popolato raw.market_id).

        Un epic non valido nella subscription MERGE fa fallire l'intero canale con
        'Insufficient permissions', quindi gli epic non risolti vanno esclusi.
        """
        return [i.epic for i in self.registry.all() if (i.raw or {}).get("market_id")]

    async def _start_stream(self) -> None:
        try:
            from market.streaming import IGStreamingClient

            session = await self.ig_client.authenticate()
            epics = self._streamable_epics()
            if not epics:
                log.warning("ig.stream.no_validated_epics", note="nessun epic validato: stream saltato, prezzi via REST")
                return
            self.stream = IGStreamingClient(session, self.prices)  # type: ignore[arg-type]
            await self.stream.start(epics)
        except Exception as exc:  # noqa: BLE001
            log.warning("ig.stream.unavailable", error=str(exc)[:160])
            self.stream = None

    # ------------------------------------------------------------ handlers
    async def _on_news(self, event: BusEvent) -> None:
        detected = await self.detector.on_news(event)
        if detected:
            await self._dispatch(detected)

    async def _on_macro(self, event: BusEvent) -> None:
        detected = await self.detector.on_macro(event)
        if detected:
            await self._dispatch(detected)

    async def _on_anomaly(self, event: BusEvent) -> None:
        detected = await self.detector.on_anomaly(event)
        if detected:
            await self._dispatch(detected)

    async def _on_event_detected(self, event: BusEvent) -> None:
        return None

    async def _dispatch(self, detected: DetectedEvent) -> None:
        # news-latency Limitless: eventi affidabili giudicano subito il long-tail che matcha
        if getattr(self, "limitless_loop", None) is not None and self.settings.autonomy_level.value > 0:

            async def news_gated() -> Any:
                async with self._pipeline_slots:
                    return await self.limitless_loop.on_event(detected)

            self.tasks.append(asyncio.create_task(news_gated()))
            self.tasks = [t for t in self.tasks if not t.done()]
        if self.pipeline is None:
            return
        if not self.settings.ig_enabled:
            return  # pipeline event->asset e' IG-centrica: spenta insieme a IG
        if self.settings.autonomy_level.value == 0:
            return

        async def gated() -> Any:
            async with self._pipeline_slots:
                if detected.age_seconds > 3600:
                    return None  # in coda troppo a lungo: l'edge event-driven e' sparito
                return await self.pipeline.handle_event(detected)  # type: ignore[union-attr]

        self.tasks.append(asyncio.create_task(gated()))
        self.tasks = [t for t in self.tasks if not t.done()]

    # --------------------------------------------------------------- loops
    async def _loop(self, name: str, interval_s: float, fn: Any, *args: Any, **kwargs: Any) -> None:
        backoff = interval_s
        while True:
            try:
                result = await fn(*args, **kwargs)
                self.status[name] = {"last_run": utcnow().isoformat(), "result": result if isinstance(result, (int, float, str, dict)) else str(result)[:200]}
                backoff = interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status[name] = {"last_run": utcnow().isoformat(), "error": str(exc)[:200]}
                log.error("loop.error", loop=name, error=str(exc)[:200])
                backoff = min(backoff * 2, 600)
            await asyncio.sleep(backoff)

    async def _scan_polymarket(self) -> int:
        events = await self.detector.scan_polymarket_repricing()
        for event in events:
            await self._dispatch(event)
        clusters = await self.detector.detect_wallet_cluster()
        for event in clusters:
            await self._dispatch(event)
        return len(events) + len(clusters)

    async def _evaluate(self) -> dict[str, Any]:
        assert self.prices and self.engine
        resolved = await resolve_pending_predictions(self.prices)
        snapshot = await self.engine.snapshot_portfolio()
        from evaluation.calibration import update_calibration
        from evaluation.metrics import failure_detection

        calibration = await update_calibration("trade")
        failures = await failure_detection(mode=self.engine.mode.value)
        if any(f["type"] == "execution_deterioration" for f in failures["flags"]) and self.engine.mode.uses_real_money:
            await self.kill_switch.trigger(KillSwitchReason.REPEATED_REJECTED_ORDERS, by="evaluation", flags=failures["flags"])
        return {"predictions_resolved": resolved, "equity": snapshot["equity"], "calibration_n": calibration.get("n", 0), "failure_flags": len(failures["flags"])}

    async def _reconcile(self) -> dict[str, Any] | None:
        if self.ig_client is None or not self.settings.execution_mode.sends_orders_to_broker:
            return None
        from execution.ig.reconciliation import Reconciler

        report = await Reconciler(self.ig_client, mode=self.settings.execution_mode.value, kill_switch=self.kill_switch).run()
        return report.as_dict()

    async def _health(self) -> dict[str, Any]:
        assert self.prices
        stale = [epic for epic, q in ((e, self.prices.cached(e)) for e in [i.epic for i in self.registry.all()]) if q is None or q.age_seconds() > 300]
        if self.settings.execution_mode.sends_orders_to_broker and self.ig_client is not None and not self.ig_client.healthy:
            await self.kill_switch.trigger(KillSwitchReason.API_UNAVAILABLE, by="health")
        return {"stale_quotes": len(stale), "kill_switch": self.kill_switch.snapshot(), "ig": self.ig_client.healthy if self.ig_client else None, "stream": self.stream.healthy if self.stream else None}

    async def run(self) -> None:
        await self.setup()
        assert self.engine and self.monitor
        collectors = self.collectors
        # Backfill storico via REST solo senza IG (dati pubblici); con IG connesso la serie la
        # costruisce lo streaming Lightstreamer, evitando di consumare l'allowance del conto demo.
        if self.settings.ig_enabled and self.ig_client is None:
            await collectors["ig_prices"].run_once(CollectionMode.HISTORICAL_BATCH, minutes=24 * 60)
        ig_price_interval = 30.0 if self.ig_client is not None else 15.0
        loops = [
            self._loop("news_rss", self.settings.news.poll_interval_s, collectors["news_rss"].run_once),
            self._loop("macro_calendar", self.settings.macro.poll_interval_s, collectors["macro_calendar"].run_once),
            self._loop("polymarket_markets", 60.0, collectors["polymarket_markets"].run_once, CollectionMode.INCREMENTAL, limit=120),
            self._loop("polymarket_wallets", 300.0, collectors["polymarket_wallets"].run_once),
            self._loop("polymarket_scan", 90.0, self._scan_polymarket),
            self._loop("monitor", 10.0, self.monitor.tick),
            self._loop("evaluation", 300.0, self._evaluate),
            self._loop("health", 30.0, self._health),
        ]
        if self.settings.ig_enabled:
            loops.append(self._loop("ig_prices", ig_price_interval, collectors["ig_prices"].run_once))
            loops.append(self._loop("reconcile", self.settings.ig.reconcile_interval_s, self._reconcile))
        if "limitless_markets" in collectors:
            await collectors["limitless_markets"].run_once()  # primo scan subito: candidati pronti
            loops.append(self._loop("limitless_markets", self.settings.limitless.scan_interval_s, collectors["limitless_markets"].run_once))
            loops.append(self._loop("limitless_decide", self.settings.limitless.judge_interval_s, self.limitless_loop.cycle))
            cfg = self.settings.limitless
            if cfg.maker and cfg.clob_api_key and cfg.clob_api_secret and cfg.private_key:
                from execution.limitless.maker import CompleteSetMaker

                self.maker = CompleteSetMaker(
                    api_key=cfg.clob_api_key.get_secret_value(), api_secret=cfg.clob_api_secret.get_secret_value(),
                    private_key=cfg.private_key.get_secret_value())
                loops.append(self._loop("limitless_maker", 20.0, self.maker.tick))
                log.info("limitless.maker.enabled", pair_target=self.maker.pair_target, size=self.maker.size_usdc)
        self.tasks.extend(asyncio.create_task(loop) for loop in loops)
        if self.settings.api_enabled:
            self.tasks.append(asyncio.create_task(self._serve_api()))
        log.info("runner.started", mode=self.settings.execution_mode.value, autonomy=self.settings.autonomy_level.value, ig=self.ig_client is not None, api_port=self.settings.api_port if self.settings.api_enabled else None)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await self.shutdown()

    async def _serve_api(self) -> None:
        """Dashboard/API nello stesso processo: i controlli umani agiscono sull'engine vivo."""
        import uvicorn

        from api.app import app, attach

        attach(engine=self.engine, runner=self)
        config = uvicorn.Config(app, host=self.settings.api_host, port=self.settings.api_port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        await server.serve()

    async def shutdown(self) -> None:
        log.info("runner.stopping")
        with contextlib.suppress(Exception):
            await (await get_bus(self.settings.redis_url)).stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.stream:
            await self.stream.stop()
        for collector in self.collectors.values():
            with contextlib.suppress(Exception):
                await collector.aclose()
        if self.ig_client:
            with contextlib.suppress(Exception):
                await self.ig_client.aclose()

    def snapshot(self) -> dict[str, Any]:
        return {"started_at": self.started_at.isoformat(), "uptime_s": (utcnow() - self.started_at).total_seconds(), "mode": self.settings.execution_mode.value, "autonomy": self.settings.autonomy_level.value, "ig_connected": self.ig_client is not None, "stream": self.stream.healthy if self.stream else None, "loops": self.status, "collectors": {k: v.stats.snapshot() for k, v in self.collectors.items()}, "pipeline_recent": [o.as_dict() for o in (self.pipeline.history[-20:] if self.pipeline else [])], "kill_switch": self.kill_switch.snapshot()}


def main() -> None:
    asyncio.run(Runner().run())


if __name__ == "__main__":
    main()
