"""Verifica connettivita IG (DoD patch sez. 40: auth DEMO, universo, EPIC, prezzi live, conto)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config import get_settings  # noqa: E402
from core.enums import IGEnvironment  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from execution.ig.client import IGClient  # noqa: E402
from market.instrument_registry import get_registry  # noqa: E402


async def main() -> None:
    configure_logging("INFO")
    settings = get_settings()
    env = IGEnvironment.DEMO
    if not settings.ig.configured(env):
        print("IG DEMO non configurato (ATS_IG_DEMO_API_KEY/USERNAME/PASSWORD)")
        return
    client = IGClient(env)
    session = await client.authenticate()
    print("login ok:", session.account_id, session.currency, session.account_type, "LS:", session.lightstreamerEndpoint if hasattr(session, "lightstreamerEndpoint") else session.lightstreamer_endpoint)
    account = await client.get_account()
    print("conto:", account.get("balance"))
    report = await get_registry().sync_from_ig(client)
    print("registry sync:", {k: (v if k != "updated" else len(v)) for k, v in report.items()})
    for inst in get_registry().all()[:6]:
        snap = await client.get_prices(inst.epic)
        print(f"  {inst.name:22s} {inst.epic:28s} bid={snap.get('bid')} offer={snap.get('offer')} status={snap.get('marketStatus')}")
    positions = await client.get_positions()
    print("posizioni aperte su IG:", len(positions))
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
