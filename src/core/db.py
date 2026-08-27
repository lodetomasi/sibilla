"""Engine e sessioni async (sez. 43/45)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings
from core.logging import get_logger
from core.models import Base

log = get_logger("core.db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        # piu loop asyncio scrivono in concorrenza: WAL + busy timeout evitano "database is locked"
        return {"echo": False, "future": True, "connect_args": {"timeout": 30}, "pool_size": 20, "max_overflow": 40, "pool_timeout": 60}
    return {
        "echo": False,
        "future": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }


def get_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        target = url or get_settings().database_url
        _engine = create_async_engine(target, **_engine_kwargs(target))
        if target.startswith("sqlite"):
            from sqlalchemy import event as sa_event

            @sa_event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
                # WAL: i lettori (API/dashboard) prendono uno snapshot immediato e non
                # vengono mai bloccati dagli scrittori. La serializzazione degli scrittori
                # (unico processo) e' garantita da _WRITE_LOCK in session_scope, cosi non
                # servono BEGIN IMMEDIATE ne si rischia il deadlock di upgrade read->write.
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        log.info("db.engine.created", url=target.split("@")[-1])
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


# Serializza gli scrittori nel singolo processo: con SQLite un solo writer per volta
# evita "database is locked" senza forzare BEGIN IMMEDIATE (che bloccherebbe i lettori).
_WRITE_LOCK = asyncio.Lock()


@asynccontextmanager
async def session_scope(*, write: bool = True) -> AsyncIterator[AsyncSession]:
    """Sessione transazionale: commit su successo, rollback su errore.

    write=True (default): acquisisce il lock di scrittura di processo -> gli scrittori
    non si pestano i piedi. write=False: sola lettura (API/dashboard), nessun lock,
    snapshot WAL immediato e non bloccante anche mentre gli scrittori lavorano.
    """
    factory = get_session_factory()
    if write:
        async with _WRITE_LOCK, factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    else:
        # sola lettura: nessun rollback (scaderebbe gli oggetti ORM rendendoli inutilizzabili
        # dopo la chiusura del blocco); la sessione viene solo chiusa.
        async with factory() as session:
            yield session


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependency FastAPI (sola lettura, non bloccante)."""
    async with session_scope(write=False) as session:
        yield session


async def create_all(url: str | None = None) -> None:
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("db.schema.created", tables=len(Base.metadata.tables))


async def drop_all(url: str | None = None) -> None:
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset_engine_for_tests(engine: AsyncEngine, factory: async_sessionmaker[AsyncSession]) -> None:
    global _engine, _session_factory
    _engine = engine
    _session_factory = factory


TIMESCALE_HYPERTABLES = ("market_prices", "orderbooks")


async def setup_timescale(url: str | None = None) -> list[str]:
    """Converte le serie temporali in hypertable se TimescaleDB e disponibile."""
    from sqlalchemy import text

    engine = get_engine(url)
    if engine.dialect.name != "postgresql":
        return []
    applied: list[str] = []
    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        except Exception as exc:  # noqa: BLE001
            log.info("db.timescale.unavailable", error=str(exc)[:120])
            return []
        for table in TIMESCALE_HYPERTABLES:
            try:
                await conn.execute(
                    text(
                        f"SELECT create_hypertable('{table}', 'ts', "
                        "if_not_exists => TRUE, migrate_data => TRUE)"
                    )
                )
                applied.append(table)
            except Exception as exc:  # noqa: BLE001
                log.warning("db.timescale.hypertable_failed", table=table, error=str(exc)[:120])
    return applied
