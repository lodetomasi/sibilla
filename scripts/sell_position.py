"""Vendita anticipata di quote su un pool FPMM Limitless (Base).

Uso: PYTHONPATH=src python scripts/sell_position.py <fpmm> <outcome_index> [slippage_bps]
Vende TUTTO il saldo del wallet bot per quell'outcome: approva il CTF verso l'FPMM
(una tantum) e chiama sell() col massimo ritorno che il pool paga per le nostre quote.
Exit quando la tesi e' invalidata: senza edge, niente posizione.
"""
from __future__ import annotations

import os
import sys

from web3 import Web3

from execution.limitless.maker import CTF
from execution.limitless.onchain import USDC

FPMM_ABI = [
    {"name": "calcSellAmount", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "returnAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"}],
     "outputs": [{"name": "outcomeTokenSellAmount", "type": "uint256"}]},
    {"name": "sell", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "returnAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"},
                {"name": "maxOutcomeTokensToSell", "type": "uint256"}], "outputs": []},
    {"name": "conditionIds", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}], "outputs": [{"type": "bytes32"}]},
]
CTF_ABI = [
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "outputs": []},
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"type": "bool"}]},
    {"name": "getCollectionId", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "p", "type": "bytes32"}, {"name": "c", "type": "bytes32"}, {"name": "i", "type": "uint256"}],
     "outputs": [{"type": "bytes32"}]},
    {"name": "getPositionId", "type": "function", "stateMutability": "pure",
     "inputs": [{"name": "col", "type": "address"}, {"name": "collection", "type": "bytes32"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
]


def send(w3: Web3, acct, fn, nonce: int) -> str:
    tx = fn.build_transaction({"from": acct.address, "nonce": nonce,
                               "maxFeePerGas": w3.eth.gas_price * 2,
                               "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"), "chainId": 8453})
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"tx fallita: {h.hex()}")
    return h.hex()


def main() -> None:
    from eth_account import Account
    fpmm_addr = Web3.to_checksum_address(sys.argv[1])
    outcome = int(sys.argv[2])
    slippage_bps = int(sys.argv[3]) if len(sys.argv) > 3 else 150
    acct = Account.from_key(os.environ["ATS_LIMITLESS_PRIVATE_KEY"])
    # letture su publicnode, transazioni SOLO su mainnet.base.org (lezione pagata)
    w3r = Web3(Web3.HTTPProvider("https://base-rpc.publicnode.com", request_kwargs={"timeout": 15}))
    w3t = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 30}))
    fpmm_r = w3r.eth.contract(address=fpmm_addr, abi=FPMM_ABI)
    ctf_r = w3r.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)

    cond = fpmm_r.functions.conditionIds(0).call()
    col = ctf_r.functions.getCollectionId(b"\x00" * 32, cond, 1 << outcome).call()
    pid = ctf_r.functions.getPositionId(Web3.to_checksum_address(USDC), col).call()
    bal = ctf_r.functions.balanceOf(acct.address, pid).call()
    print(f"saldo outcome {outcome}: {bal / 1e6:.4f} quote (pid {str(pid)[:16]}…)")
    if bal < 10_000:
        print("nulla da vendere")
        return

    # massimo returnAmount tale che calcSellAmount(return) <= saldo (ricerca binaria)
    lo, hi = 0, bal  # il ritorno non puo' superare 1 USDC/quota
    while hi - lo > 1_000:  # precisione 0.001 USDC
        mid = (lo + hi) // 2
        try:
            need = fpmm_r.functions.calcSellAmount(mid, outcome).call()
        except Exception:
            hi = mid
            continue
        if need <= bal:
            lo = mid
        else:
            hi = mid
    ret = lo * (10_000 - slippage_bps) // 10_000
    max_tokens = bal
    print(f"vendo fino a {bal / 1e6:.4f} quote per >= {ret / 1e6:.4f} USDC (slippage {slippage_bps}bps)")

    # nonce: MAI fidarsi di un solo RPC (nodi dietro LB con stato vecchio): si prende il massimo
    nonce = max(w3t.eth.get_transaction_count(acct.address, "pending"),
                w3r.eth.get_transaction_count(acct.address, "pending"))
    if not ctf_r.functions.isApprovedForAll(acct.address, fpmm_addr).call():
        fn = w3t.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI).functions.setApprovalForAll(fpmm_addr, True)
        print("approve CTF→FPMM:", send(w3t, acct, fn, nonce))
        nonce += 1
    fn = w3t.eth.contract(address=fpmm_addr, abi=FPMM_ABI).functions.sell(ret, outcome, max_tokens)
    print("SELL tx:", send(w3t, acct, fn, nonce))
    print(f"incassati ~{ret / 1e6:.2f} USDC")


if __name__ == "__main__":
    main()
