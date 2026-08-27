"""Onboarding/verifica wallet bot Limitless (self-custody, SDK modulare).

- genera una chiave EOA SE non esiste gia in data/limitless_secrets.env (mai stampata);
- verifica auth HMAC (profilo account) e istanzia OrderClient col wallet bot;
- stampa SOLO indirizzi pubblici. I fondi (USDC su Base) li deposita l'utente.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eth_account import Account  # noqa: E402

SECRETS = Path("data/limitless_secrets.env")


def load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    if SECRETS.exists():
        for m in re.finditer(r"^(?:export )?([A-Z_]+)=(\S+)$", SECRETS.read_text(), re.M):
            out[m.group(1)] = m.group(2)
    return out


def load_or_create_key(secrets: dict[str, str]) -> tuple[str, str]:
    key = secrets.get("ATS_LIMITLESS_PRIVATE_KEY")
    if key:
        return key, Account.from_key(key).address
    acct = Account.create()
    key = acct.key.hex()
    os.umask(0o077)
    with SECRETS.open("a") as f:
        f.write(f"\nexport ATS_LIMITLESS_PRIVATE_KEY={key}\nexport ATS_LIMITLESS_WALLET_ADDRESS={acct.address}\n")
    SECRETS.chmod(0o600)
    return key, acct.address


async def main() -> None:
    from limitless_sdk.orders import OrderClient
    from limitless_sdk.sdk_client import Client
    from limitless_sdk.types.api_tokens import HMACCredentials

    secrets = load_secrets()
    key, address = load_or_create_key(secrets)
    print(f"== Wallet bot dedicato (EOA self-custody): {address}")

    api_key = os.environ.get("ATS_LIMITLESS_API_KEY") or secrets.get("ATS_LIMITLESS_API_KEY")
    api_secret = os.environ.get("ATS_LIMITLESS_API_SECRET") or secrets.get("ATS_LIMITLESS_API_SECRET")
    if not (api_key and api_secret):
        print("== Credenziali HMAC assenti: salta verifica profilo.")
        return

    creds = HMACCredentials(tokenId=api_key, secret=api_secret)
    async with Client(hmac_credentials=creds) as client:
        prof = await client.portfolio.get_current_profile()
        acct = prof.get("account") if isinstance(prof, dict) else getattr(prof, "account", None)
        print(f"== AUTH HMAC OK | account Limitless: {acct}")
        wallet = Account.from_key(key)
        OrderClient(client.http, wallet, market_fetcher=client.markets)
        print(f"== OrderClient pronto | maker: {wallet.address}")
        if acct and str(acct).lower() != wallet.address.lower():
            print("!! NOTA: il token HMAC appartiene all'account "
                  f"{acct}, non al wallet bot: per ordinare col wallet bot serve "
                  "un API token derivato dal wallet bot (client.api_tokens.derive_token).")

    print(f"\n>> DEPOSITA QUI (rete Base): USDC per operare + un po' di ETH per il gas -> {address}")


if __name__ == "__main__":
    asyncio.run(main())
