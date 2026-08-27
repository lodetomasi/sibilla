"""Report giornaliero del desk: P&L on-chain verificabile + calibrazione AI + ciclo maker.

Barra dritta: questi numeri (non le sensazioni) decidono se scalare il capitale.
Uso: PYTHONPATH=src .venv/bin/python scripts/report_desk.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

RPC = "https://base-rpc.publicnode.com"
CTF = "0xC9c98965297Bc527861c898329Ee280632B76e18"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WALLET = "0x9BF9F4eD7C0538531432980643E3456fB7A93D13"
DATA = Path(__file__).resolve().parents[1] / "data"


def rpc_call(client: httpx.Client, to: str, data: str) -> int:
    r = client.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                               "params": [{"to": to, "data": data}, "latest"]}, timeout=15)
    r.raise_for_status()
    out = r.json()
    if "error" in out:
        raise RuntimeError(out["error"])
    return int(out["result"], 16)


def balance_of_ctf(client: httpx.Client, pid: str) -> float:
    # balanceOf(address,uint256) -> 0x00fdd58e
    data = ("0x00fdd58e" + WALLET.lower()[2:].rjust(64, "0") + format(int(pid), "064x"))
    return rpc_call(client, CTF, data) / 1e6


def main(total_only: bool = False) -> None:
    with httpx.Client() as client:
        cash = rpc_call(client, USDC, "0x70a08231" + WALLET.lower()[2:].rjust(64, "0")) / 1e6

        # inventario maker: set completi marcati 1.00 (garantiti), sbilanci 0.50 (coin-flip)
        reg = json.loads((DATA / "maker_touched.json").read_text()) if (DATA / "maker_touched.json").exists() else {}
        sets_value = singles_value = 0.0
        rows = []
        for slug, v in sorted(reg.items(), key=lambda kv: kv[1].get("expiry", "")):
            pids = (v.get("position_ids") or [])[:2]
            if len(pids) < 2:
                continue
            try:
                y, n = balance_of_ctf(client, pids[0]), balance_of_ctf(client, pids[1])
            except Exception:  # noqa: BLE001 - RPC ko: salta, mai inventare
                rows.append((slug, "rpc-err", "rpc-err"))
                continue
            if y + n < 0.01:
                continue
            sets_value += min(y, n) * 1.0
            singles_value += abs(y - n) * 0.5
            rows.append((slug, round(y, 2), round(n, 2)))

    rp = json.loads((DATA / "real_positions.json").read_text())
    baseline = float(rp.get("baseline_usdc") or 0)
    amm_spent = sum(float(p.get("usdc_spent") or 0) for p in rp.get("positions", []))
    amm_shares = sum(float(p.get("shares") or 0) for p in rp.get("positions", []))
    amm_mark = amm_shares * 0.5  # mark prudente; la verita' e' alla risoluzione

    if total_only:
        total = cash + sets_value + singles_value + amm_mark
        print(f"MARKER total={total:.2f}")
        return
    print(f"CASH on-chain           : {cash:8.2f} USDC")
    print(f"Set completi (garantiti): {sets_value:8.2f}")
    print(f"Sbilanci maker (mark .5): {singles_value:8.2f}")
    print(f"AMM shares {amm_shares:6.2f} (spesi {amm_spent:.2f}, mark .5): {amm_mark:8.2f}")
    total = cash + sets_value + singles_value + amm_mark
    print(f"TOTALE mark-to-market   : {total:8.2f}  vs baseline {baseline:.2f}  ->  P&L {total - baseline:+.2f}")
    for slug, y, n in rows:
        print(f"  inv {slug[:50]:50} YES={y} NO={n}")

    # calibrazione AI: esiti del journal (il gate dei 25 trade)
    db = DATA / "ats.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        try:
            cur = con.execute(
                "select outcome, count(*) from trade_journal "
                "where ts > datetime('now','-3 day') group by outcome order by 2 desc")
            print("Journal (3gg):", {r[0]: r[1] for r in cur.fetchall()})
        except sqlite3.Error as exc:
            print("journal non leggibile:", exc)
        finally:
            con.close()


if __name__ == "__main__":
    import sys
    main(total_only="--total" in sys.argv)
