"""Legge il saldo ETH e USDC del wallet bot sulla rete Base (RPC pubblico, read-only).

Serve a vedere quando i fondi depositati arrivano. Nessuna chiave usata: solo lettura on-chain.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

BASE_RPC = "https://mainnet.base.org"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC nativo su Base (6 decimali)
SECRETS = Path(__file__).resolve().parents[1] / "data" / "limitless_secrets.env"
FALLBACK = "0x9BF9F4eD7C0538531432980643E3456fB7A93D13"


def wallet_address() -> str:
    if len(sys.argv) > 1 and sys.argv[1].startswith("0x"):
        return sys.argv[1]
    if SECRETS.exists():
        m = re.search(r"ATS_LIMITLESS_WALLET_ADDRESS=(\S+)", SECRETS.read_text())
        if m:
            return m.group(1)
    return FALLBACK


def rpc(method: str, params: list) -> str:
    r = httpx.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    out = r.json()
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def main() -> None:
    addr = wallet_address()
    print(f"Wallet bot (Base): {addr}")
    eth_wei = int(rpc("eth_getBalance", [addr, "latest"]), 16)
    eth = eth_wei / 1e18
    # balanceOf(address) -> selector 0x70a08231 + address left-padded a 32 byte
    data = "0x70a08231" + addr.lower().replace("0x", "").rjust(64, "0")
    usdc_raw = int(rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"]), 16)
    usdc = usdc_raw / 1e6
    print(f"  ETH:  {eth:.6f}   ({'ok per il gas' if eth > 0.0003 else 'INSUFFICIENTE per il gas: manda ~$1-2 di ETH su Base'})")
    print(f"  USDC: {usdc:.2f}   ({'PRONTO A OPERARE' if usdc >= 1 else 'in attesa di deposito USDC su Base'})")
    ready = "yes" if usdc >= 1 else "no"
    print(f"MARKER ready={ready} usdc={usdc:.2f} eth={eth:.6f}")
    if usdc >= 1 and eth > 0.0003:
        print(">> Fondi presenti: allowance + primo ordine reale.")
    elif usdc >= 1:
        print(">> USDC presente ma manca ETH per il gas: manda ~$2 di ETH su Base.")
    else:
        print(f">> In attesa. Deposita USDC (+ ~$2 ETH) su rete Base a: {addr}")


if __name__ == "__main__":
    sys.exit(main())
