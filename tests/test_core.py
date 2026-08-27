from datetime import timedelta

import pytest

from core.clock import age_seconds, utcnow
from core.config import RiskLimits, load_settings
from core.enums import ExecutionMode, FreshnessBucket, SourceTier
from core.errors import ConfigError
from core.logging import scrub_text, scrub_value
from core.schemas import BookLevel, Evidence, OrderBook
from core.secrets import SecretBox


def test_freshness_buckets():
    assert FreshnessBucket.from_seconds(10) is FreshnessBucket.T_0_30S
    assert FreshnessBucket.from_seconds(55) is FreshnessBucket.T_30_120S
    assert FreshnessBucket.from_seconds(200) is FreshnessBucket.T_2_5M
    assert FreshnessBucket.from_seconds(1000) is FreshnessBucket.T_5_30M
    assert FreshnessBucket.from_seconds(5000) is FreshnessBucket.T_OVER_30M
    assert FreshnessBucket.T_0_30S.weight > FreshnessBucket.T_OVER_30M.weight


def test_source_tier_reliability_monotona():
    tiers = [SourceTier.TIER_1, SourceTier.TIER_2, SourceTier.TIER_3, SourceTier.TIER_4, SourceTier.TIER_5]
    values = [t.reliability for t in tiers]
    assert values == sorted(values, reverse=True)


def test_risk_limits_vietano_full_kelly():
    with pytest.raises(ValueError):
        RiskLimits(kelly_fraction=1.0)
    with pytest.raises(ValueError):
        RiskLimits(kelly_fraction=0.5)
    assert RiskLimits(kelly_fraction=0.25).kelly_fraction == 0.25


def test_risk_limits_immutabili():
    limits = RiskLimits()
    with pytest.raises(Exception):
        limits.max_trade_risk = 0.5  # frozen: nessun LLM puo modificarli


def test_execution_mode_semantica():
    assert not ExecutionMode.SHADOW.sends_orders_to_broker
    assert not ExecutionMode.PAPER.sends_orders_to_broker
    assert ExecutionMode.DEMO.sends_orders_to_broker and not ExecutionMode.DEMO.uses_real_money
    assert ExecutionMode.LIVE_SMALL.uses_real_money and ExecutionMode.LIVE.uses_real_money
    assert ExecutionMode.DEMO.ig_environment.value == "DEMO" and ExecutionMode.LIVE.ig_environment.value == "LIVE"


def test_settings_flat_override(monkeypatch):
    monkeypatch.setenv("ATS_MAX_RISK_PER_TRADE", "0.004")
    monkeypatch.setenv("ATS_BANKROLL", "500")
    monkeypatch.setenv("ATS_IG_DEMO_API_KEY", "demo-key-x")
    settings = load_settings(redis_url=None)
    assert settings.risk.max_risk_per_trade == 0.004
    assert settings.risk.bankroll == 500
    assert settings.ig.demo.api_key.get_secret_value() == "demo-key-x"
    assert settings.ig.live.api_key is None or settings.ig.live.api_key.get_secret_value() != "demo-key-x"


def test_scrub_secrets():
    assert "hunter2" not in scrub_text("password=hunter2secret")
    assert "sk-ant-abcdefghijk" not in scrub_text("key sk-ant-abcdefghijk123")
    scrubbed = scrub_value({"api_key": "abc123", "keep": "ok"})
    assert scrubbed["api_key"] != "abc123"
    assert scrubbed["keep"] == "ok"


def test_secret_box_roundtrip():
    box = SecretBox(SecretBox.generate_key())
    assert box.decrypt(box.encrypt("valore")) == "valore"
    with pytest.raises(ConfigError):
        SecretBox("")


def test_secret_box_passphrase_arbitraria():
    box = SecretBox("passphrase-non-base64")
    assert box.decrypt(box.encrypt("x")) == "x"


def test_clock_congelato_e_ripristinato(clock):
    assert utcnow() == clock.now()
    clock.advance(seconds=30)
    assert age_seconds(clock.now() - timedelta(seconds=30)) == pytest.approx(30, abs=0.01)


def test_orderbook_probabilita_polymarket():
    """Convenzione probability: asks = si compra (BACK), prezzo basso e' meglio."""
    book = OrderBook(
        venue="polymarket",
        market_id="0xabc",
        bids=[BookLevel(price=0.60, size=100), BookLevel(price=0.59, size=50)],
        asks=[BookLevel(price=0.62, size=40), BookLevel(price=0.63, size=60)],
    ).sort_levels()
    assert book.best_ask == 0.62  # miglior prezzo per BACK
    assert book.best_bid == 0.60  # miglior prezzo per LAY
    assert book.spread == pytest.approx(0.02)
    assert book.mid == pytest.approx(0.61)
    assert book.implied_probability == pytest.approx(0.61)
    assert book.liquidity_at_or_better(0.62, "BACK") == 40
    assert book.liquidity_at_or_better(0.63, "BACK") == 100
    assert book.liquidity_at_or_better(0.59, "LAY") == 150


def test_evidence_freshness(clock):
    evidence = Evidence(
        evidence_id="e1",
        type="NEWS",
        source="Reuters",
        timestamp=clock.now() - timedelta(seconds=45),
    )
    assert evidence.age_seconds == pytest.approx(45, abs=1)
    assert evidence.freshness is FreshnessBucket.T_30_120S


def test_evidence_richiede_timestamp():
    with pytest.raises(Exception):
        Evidence(evidence_id="e1", type="NEWS", source="X")  # type: ignore[call-arg]


def test_pricing_cfd():
    from core.enums import Direction
    from core.pricing import (
        entry_price,
        exit_price,
        is_price_acceptable,
        limit_level,
        margin_required,
        notional,
        pnl_money,
        reward_risk_ratio,
        round_to_step,
        size_from_risk,
        stop_level,
        worse_price,
    )

    assert entry_price(99.9, 100.1, "BUY") == 100.1 and entry_price(99.9, 100.1, "SELL") == 99.9
    assert exit_price(99.9, 100.1, "BUY") == 99.9
    assert is_price_acceptable(100.0, 100.05, None, "BUY") and not is_price_acceptable(100.1, 100.05, None, "BUY")  # type: ignore[arg-type]
    assert is_price_acceptable(100.0, 99.95, None, "SELL") and not is_price_acceptable(99.9, 99.95, None, "SELL")  # type: ignore[arg-type]
    assert worse_price(100.0, 0.001, "BUY") == pytest.approx(100.1) and worse_price(100.0, 0.001, "SELL") == pytest.approx(99.9)
    assert stop_level(100.0, 2.0, "BUY") == 98.0 and stop_level(100.0, 2.0, "SELL") == 102.0
    assert limit_level(100.0, 3.0, "BUY") == 103.0
    assert pnl_money(100.0, 101.0, "BUY", 2.0, 1.0) == pytest.approx(2.0)
    assert pnl_money(100.0, 101.0, "SELL", 2.0, 1.0) == pytest.approx(-2.0)
    assert size_from_risk(2.5, 0.5, 1.0) == pytest.approx(5.0)
    assert notional(20000, 0.1, 1.0) == pytest.approx(2000) and margin_required(20000, 0.1, 1.0, 5.0) == pytest.approx(100)
    assert reward_risk_ratio(100, 50) == 2.0
    assert round_to_step(0.37, 0.1, direction="down") == pytest.approx(0.3)
    assert Direction.parse("LONG") is Direction.BUY and Direction.BUY.opposite is Direction.SELL


async def test_bus_asincrono_non_blocca_il_publisher(bus):
    """Un handler lento/che scrive non deve bloccare publish (evita deadlock SQLite)."""
    import asyncio

    from core.bus import BusEvent
    from core.enums import EventType

    order: list[str] = []

    async def slow_handler(event: BusEvent) -> None:
        await asyncio.sleep(0.05)
        order.append("handler")

    bus.subscribe(EventType.PRICE_CHANGED, slow_handler)
    await bus.publish(BusEvent(type=EventType.PRICE_CHANGED, payload={}))
    order.append("publisher")  # publish ritorna prima che l'handler finisca
    await bus.drain()
    assert order == ["publisher", "handler"]
    assert bus.delivered == 1 and bus.pending == 0


async def test_bus_handler_errore_isolato(bus):
    from core.bus import BusEvent
    from core.enums import EventType

    async def bad(event: BusEvent) -> None:
        raise RuntimeError("boom")

    ok_calls: list[int] = []

    async def good(event: BusEvent) -> None:
        ok_calls.append(1)

    bus.subscribe(EventType.NEWS_DETECTED, bad)
    bus.subscribe(EventType.NEWS_DETECTED, good)
    await bus.publish(BusEvent(type=EventType.NEWS_DETECTED, payload={}))
    await bus.drain()
    assert ok_calls == [1] and bus.errors == 1
