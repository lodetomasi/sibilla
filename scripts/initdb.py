"""Crea lo schema DB (SQLite o PostgreSQL) e il registry strategie/strumenti."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.db import create_all, setup_timescale  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from market.instrument_registry import get_registry  # noqa: E402
from strategies.catalog import ensure_registry  # noqa: E402


async def main() -> None:
    configure_logging("INFO")
    await create_all()
    hyper = await setup_timescale()
    await ensure_registry()
    await get_registry().save_to_db()
    print(f"schema creato; hypertable: {hyper or 'n/a'}; strumenti: {len(get_registry().all())}")


if __name__ == "__main__":
    asyncio.run(main())
