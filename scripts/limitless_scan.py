"""Scanner Limitless: mostra mercati YES/NO reali (generalisti) con prezzo implicito e volume.

Read-only, niente wallet. Serve a vedere su cosa il sistema potrebbe operare e a
confrontare la probabilita di mercato con quella del modello (edge = model - market).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.logging import configure_logging  # noqa: E402
from execution.limitless.client import LimitlessClient, parse_market  # noqa: E402


async def main() -> None:
    configure_logging("ERROR")
    client = LimitlessClient()
    total = await client.active_markets(limit=1)
    markets = await client.generalist_markets(pages=10)
    parsed = [parse_market(m) for m in markets if m.get("prices")]
    parsed = [p for p in parsed if p["yes_price"] and 0.02 < p["yes_price"] < 0.98]
    parsed.sort(key=lambda p: -(p["volume"] or 0))
    print("\n== Limitless: mercati generalisti tradabili (YES/NO), ordinati per volume — mostro i primi 20")
    print(f"{'MERCATO':<58} {'YES':>6} {'NO':>6} {'VOL$':>10}  CATEGORIE")
    for p in parsed[:20]:
        cats = ",".join(str(c) for c in (p["categories"] or [])[:2])
        print(f"{str(p['title'])[:56]:<58} {p['yes_price']:>6.2f} {p['no_price']:>6.2f} {(p['volume'] or 0):>10,.0f}  {cats}")
    print(f"\n   totale mercati generalisti con prezzo valido: {len(parsed)}")
    print("   collateral:", {p['collateral'] for p in parsed[:20]})
    print("\n   Prossimo passo per operare reale: wallet Base + USDC + chiave (in sicurezza) -> executor EIP-712.")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
