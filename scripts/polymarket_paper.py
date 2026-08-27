"""Paper trading su Polymarket: compra/vende sul BOOK REALE (nessun account, nessun soldo).

Dimostra la meccanica compra->gestisci->P&L usando prezzi/liquidita veri via il client
pubblico Polymarket. Regola d'ingresso onesta: segue il consenso dei wallet qualificati
sul mercato se disponibile; altrimenti mostra la meccanica sul mercato piu liquido.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collectors.polymarket.client import PolymarketClient  # noqa: E402
from collectors.polymarket.parsers import parse_book, parse_market  # noqa: E402
from core.logging import configure_logging  # noqa: E402

STAKE_USDC = 20.0
FINANCIAL_TAGS = ("fed", "rates", "cpi", "inflation", "recession", "economy", "election", "president", "crypto", "bitcoin", "ethereum", "geopolitics", "war", "tariff")


async def main() -> None:
    configure_logging("ERROR")
    client = PolymarketClient()
    print("\n== Cerco mercati Polymarket liquidi (categoria finanziaria) sul book reale...")
    raw = await client.list_markets(limit=60, active=True, closed=False, order="volume24hr")
    cands = []
    for m in raw:
        parsed = parse_market(m)
        if parsed["category"] in ("macro", "economics", "politics", "geopolitics", "crypto", "companies") and parsed["outcomes"]:
            toks = [o for o in parsed["outcomes"] if o.get("token_id")]
            if len(toks) >= 2:
                cands.append((parsed, toks))
    if not cands:
        print("nessun mercato finanziario liquido trovato ora")
        await client.aclose()
        return
    parsed, toks = cands[0]
    print(f"\n== MERCATO: {parsed['question']}\n   categoria {parsed['category']}  volume ${parsed['volume']:,.0f}  liquidita ${parsed['liquidity']:,.0f}")

    # book reale dei due esiti
    books = await client.get_books([str(t["token_id"]) for t in toks[:2]])
    legs = []
    for tok, rawbook in zip(toks[:2], books, strict=False):
        book = parse_book(rawbook, market_id=parsed["external_id"], outcome=tok["name"])
        ask = book.best_ask  # prezzo per COMPRARE l'esito
        bid = book.best_bid  # prezzo per VENDERLO
        legs.append((tok["name"], ask, bid, book))
        print(f"   {tok['name']:>4}: compra(ask)={ask}  vendi(bid)={bid}  spread={book.spread}")

    # scelta onesta: compro l'esito con ask piu basso e book piu profondo (mostra la meccanica)
    name, ask, bid, book = min((leg for leg in legs if leg[1]), key=lambda x: x[1])
    if ask is None or ask <= 0:
        print("book non quotato ora")
        await client.aclose()
        return
    shares = STAKE_USDC / ask
    max_gain = shares * (1 - ask)   # se l'esito si risolve a 1 (SI)
    max_loss = shares * ask         # se si risolve a 0 (NO)
    print(f"\n== COMPRO (paper) {shares:.2f} share di '{name}' @ {ask:.3f}  (spesa ${STAKE_USDC:.2f})")
    print(f"   se '{name}' accade: +${max_gain:.2f}   |   se non accade: -${max_loss:.2f}   |   R:R {max_gain/max_loss:.2f}")

    # mark-to-market immediato sul book reale (uscita = vendere al bid corrente)
    exit_now = bid or (ask - book.spread if book.spread else ask)
    pnl_now = shares * (exit_now - ask)
    print(f"\n== VALORE ORA (vendendo al bid {exit_now:.3f}): P&L simulato ${pnl_now:+.2f} (include lo spread pagato)")
    print("   Nota: su Polymarket non c'e' leva; il rischio massimo e' il capitale impegnato.")
    print("   Questo e' il book REALE. Per operare con soldi veri servono: account Polymarket + wallet Polygon + USDC.")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
