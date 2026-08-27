"""Fixture comuni: DB in memoria, cache/bus in-process, clock congelato."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core import cache as cache_module
from core import db as db_module
from core.bus import InMemoryBus, reset_bus, set_bus
from core.cache import Cache, MemoryCache, set_cache
from core.clock import FrozenClock, SystemClock, set_clock
from core.config import load_settings
from core.models import Base
from core.repository import Repository

T0 = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.get_event_loop_policy()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db_module.reset_engine_for_tests(engine, factory)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    db_module._engine = None
    db_module._session_factory = None


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[object]:
    factory = db_module.get_session_factory()
    async with factory() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture
async def repo(session) -> Repository:
    return Repository(session)


@pytest.fixture
def bus() -> InMemoryBus:
    bus = InMemoryBus()
    set_bus(bus)
    yield bus
    reset_bus()


@pytest.fixture
def memory_cache() -> Cache:
    cache = Cache(MemoryCache())
    set_cache(cache)
    yield cache
    cache_module.reset_cache()


@pytest.fixture
def clock() -> FrozenClock:
    clock = FrozenClock(T0)
    set_clock(clock)
    yield clock
    set_clock(SystemClock())


@pytest.fixture
def settings():
    return load_settings(
        execution_mode="SHADOW",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url=None,
    )


@pytest.fixture
def risk_settings():
    """Settings con bankroll noto per i test di rischio."""
    return load_settings(
        execution_mode="PAPER",
        redis_url=None,
        risk={
            "bankroll": 1000.0,
            "max_trade_risk": 0.01,
            "max_event_risk": 0.02,
            "max_correlated_risk": 0.03,
            "max_total_exposure": 0.08,
            "max_daily_loss": 0.04,
            "max_weekly_drawdown": 0.08,
            "kelly_fraction": 0.15,
            "min_ev_threshold": 0.02,
            "max_stake_abs": 50.0,
            "min_stake_abs": 1.0,
        },
    )
