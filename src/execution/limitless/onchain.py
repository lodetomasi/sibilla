"""Executor on-chain per i mercati AMM di Limitless (Base, permissionless).

Il wallet bot (EOA self-custody) compra quote YES/NO direttamente sul contratto
FPMM del mercato: approve USDC -> calcBuyAmount -> buy(minShares con slippage).
Nessuno scope API richiesto. Cap rigido in USDC per ordine; il gas lo paga l'EOA.
"""
from __future__ import annotations

import asyncio
from typing import Any

from core.logging import get_logger

log = get_logger("execution.limitless.onchain")

BASE_RPC = "https://mainnet.base.org"
RPC_POOL_PRICER = "https://base-rpc.publicnode.com"   # reprice collector (80 call/90s)
RPC_MAKER = "https://base.llamarpc.com"               # pids + redeem del maker
RPC_API = "https://1rpc.io/base"                      # /api/real (letture dashboard)
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC nativo su Base (6 decimali)

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "outputs": [{"type": "bool"}]},
]
FPMM_ABI = [
    {"name": "calcBuyAmount", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "investmentAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "buy", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "investmentAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"},
                {"name": "minOutcomeTokensToBuy", "type": "uint256"}], "outputs": []},
    {"name": "calcSellAmount", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "returnAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"}],
     "outputs": [{"name": "outcomeTokenSellAmount", "type": "uint256"}]},
    {"name": "sell", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "returnAmount", "type": "uint256"}, {"name": "outcomeIndex", "type": "uint256"},
                {"name": "maxOutcomeTokensToSell", "type": "uint256"}], "outputs": []},
    {"name": "conditionIds", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}], "outputs": [{"type": "bytes32"}]},
]
CTF_ADDR = "0xC9c98965297Bc527861c898329Ee280632B76e18"  # Conditional Tokens (Base)
CTF_MIN_ABI = [
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


def min_shares(expected: int, slippage_bps: int) -> int:
    """Quote minime accettate dopo lo slippage dichiarato."""
    return expected * (10_000 - slippage_bps) // 10_000


def pool_prices_sync(w3: Any, fpmm_addr: str) -> tuple[float, float]:
    """(bid_yes, ask_yes) eseguibili dal pool FPMM: view call gratuite."""
    from web3 import Web3

    fpmm = w3.eth.contract(address=Web3.to_checksum_address(fpmm_addr), abi=FPMM_ABI)
    probe = 1_000_000
    shares_yes = fpmm.functions.calcBuyAmount(probe, 0).call()
    shares_no = fpmm.functions.calcBuyAmount(probe, 1).call()
    ask_yes = min(0.999, max(0.001, probe / max(shares_yes, 1)))
    ask_no = min(0.999, max(0.001, probe / max(shares_no, 1)))
    return min(0.999, max(0.001, 1.0 - ask_no)), ask_yes


class PoolPricer:
    """Prezzatore read-only dei pool (per il collector): nessuna chiave, solo view."""

    def __init__(self, rpc: str = RPC_POOL_PRICER):
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))

    def price(self, fpmm_addr: str) -> tuple[float, float]:
        return pool_prices_sync(self._w3, fpmm_addr)


class OnchainAmmGateway:
    """Interfaccia compatibile col gateway delegato: espone solo place_amm."""

    def __init__(self, *, private_key: str, market_client: Any, max_usdc_per_order: float = 5.0,
                 rpc: str = BASE_RPC, slippage_bps: int = 300,
                 clob_api_key: str | None = None, clob_api_secret: str | None = None):
        from eth_account import Account
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
        self._acct = Account.from_key(private_key)
        self.address = self._acct.address
        self._usdc = self._w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ERC20_ABI)
        # peer di sola lettura per il nonce: i nodi bilanciati possono restituire pending
        # stantii, quindi il nonce si prende come MASSIMO tra piu' nodi (+ contatore locale)
        self._nonce_peers = []
        for _rpc in ("https://base-rpc.publicnode.com", "https://base.drpc.org"):
            try:
                _p = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 8}))
                _p.eth.block_number
                self._nonce_peers.append(_p)
            except Exception:  # noqa: BLE001 - peer opzionale
                continue
        self._market_client = market_client
        self.max_usdc_per_order = max_usdc_per_order
        self.slippage_bps = slippage_bps
        self._fpmm_cache: dict[str, str] = {}
        # esecuzione CLOB (EIP-712 con lo stesso EOA del maker): attiva solo con le credenziali HMAC
        self._sdk = None
        self._oc = None
        self.clob_enabled = False
        if clob_api_key and clob_api_secret:
            try:
                from limitless_sdk.orders import OrderClient
                from limitless_sdk.sdk_client import Client as _LmtsClient
                from limitless_sdk.types.api_tokens import HMACCredentials
                self._sdk = _LmtsClient(hmac_credentials=HMACCredentials(tokenId=clob_api_key, secret=clob_api_secret))
                self._oc = OrderClient(self._sdk.http, self._acct, market_fetcher=self._sdk.markets)
                self.clob_enabled = True
                log.info("limitless.clob_execution.enabled", wallet=self.address)
            except Exception as exc:  # noqa: BLE001 - senza CLOB si resta AMM-only
                log.warning("limitless.clob_gateway_failed", error=str(exc)[:100])

    # ------------------------------------------------------------- sync core
    def _send(self, fn: Any) -> str:
        # nonce robusto: MASSIMO tra piu' nodi + contatore locale (i nodi bilanciati
        # possono non aver ancora indicizzato la tx precedente -> "nonce too low")
        chain_nonce = self._w3.eth.get_transaction_count(self.address, "pending")
        for peer in getattr(self, "_nonce_peers", []):
            try:
                chain_nonce = max(chain_nonce, peer.eth.get_transaction_count(self.address, "pending"))
            except Exception:  # noqa: BLE001 - peer momentaneamente giu'
                continue
        nonce = max(chain_nonce, getattr(self, "_next_nonce", 0))
        tx = fn.build_transaction({
            "from": self.address,
            "nonce": nonce,
            "maxFeePerGas": self._w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self._w3.to_wei(0.001, "gwei"),
            "chainId": 8453,
        })
        signed = self._acct.sign_transaction(tx)
        h = self._w3.eth.send_raw_transaction(signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"tx fallita: {h.hex()}")
        self._next_nonce = nonce + 1
        return h.hex()

    def _buy_sync(self, fpmm_addr: str, outcome_index: int, amount_units: int) -> dict[str, Any]:
        from web3 import Web3

        usdc_bal = self._usdc.functions.balanceOf(self.address).call()
        eth_bal = self._w3.eth.get_balance(self.address)
        if usdc_bal < amount_units:
            raise RuntimeError(f"wallet bot senza USDC sufficienti: {usdc_bal/1e6:.2f} < {amount_units/1e6:.2f} (deposita su {self.address})")
        if eth_bal < self._w3.to_wei(0.00003, "ether"):
            raise RuntimeError(f"wallet bot senza ETH per il gas su Base (deposita ~0.0005 ETH su {self.address})")
        fpmm = self._w3.eth.contract(address=Web3.to_checksum_address(fpmm_addr), abi=FPMM_ABI)
        if self._usdc.functions.allowance(self.address, fpmm.address).call() < amount_units:
            log.info("limitless.onchain.approve", fpmm=fpmm_addr)
            self._send(self._usdc.functions.approve(fpmm.address, 2**200))
        expected = fpmm.functions.calcBuyAmount(amount_units, outcome_index).call()
        floor = min_shares(expected, self.slippage_bps)
        txh = self._send(fpmm.functions.buy(amount_units, outcome_index, floor))
        return {"txHash": txh, "expectedShares": expected / 1e6, "minShares": floor / 1e6}

    def _price_sync(self, fpmm_addr: str) -> tuple[float, float]:
        return pool_prices_sync(self._w3, fpmm_addr)

    def _sell_sync(self, fpmm_addr: str, outcome_index: int) -> dict[str, Any]:
        """Vende TUTTO il saldo dell'outcome sul pool (uscita: tesi invalidata)."""
        from web3 import Web3

        fpmm = self._w3.eth.contract(address=Web3.to_checksum_address(fpmm_addr), abi=FPMM_ABI)
        ctf = self._w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDR), abi=CTF_MIN_ABI)
        cond = fpmm.functions.conditionIds(0).call()
        col = ctf.functions.getCollectionId(b"\x00" * 32, cond, 1 << outcome_index).call()
        pid = ctf.functions.getPositionId(Web3.to_checksum_address(USDC), col).call()
        bal = ctf.functions.balanceOf(self.address, pid).call()
        if bal < 10_000:
            return {"skipped": "saldo nullo"}
        lo, hi = 0, bal  # max USDC che il pool paga per <= bal quote (ricerca binaria)
        while hi - lo > 1_000:
            mid = (lo + hi) // 2
            try:
                need = fpmm.functions.calcSellAmount(mid, outcome_index).call()
            except Exception:  # noqa: BLE001
                hi = mid
                continue
            if need <= bal:
                lo = mid
            else:
                hi = mid
        ret = lo * (10_000 - self.slippage_bps) // 10_000
        if ret <= 0:
            return {"skipped": "pool senza liquidita'"}
        if not ctf.functions.isApprovedForAll(self.address, Web3.to_checksum_address(fpmm_addr)).call():
            self._send(ctf.functions.setApprovalForAll(Web3.to_checksum_address(fpmm_addr), True))
        txh = self._send(fpmm.functions.sell(ret, outcome_index, bal))
        return {"txHash": txh, "usdcReceived": ret / 1e6, "sharesSold": bal / 1e6}

    async def sell_amm(self, *, side: str, market_slug: str) -> dict[str, Any]:
        """Chiude sul pool l'intera posizione reale di un lato e ripulisce il registro."""
        fpmm_addr = await self._resolve_fpmm(market_slug)
        outcome_index = 0 if side.upper() == "YES" else 1
        out = await asyncio.to_thread(self._sell_sync, fpmm_addr, outcome_index)
        log.info("limitless.onchain.sold", market=market_slug, side=side,
                 **{k: str(v)[:66] for k, v in out.items()})
        if "txHash" in out:
            try:
                import json
                from pathlib import Path
                reg = Path("data/real_positions.json")
                data = json.loads(reg.read_text()) if reg.exists() else {"positions": []}
                data["positions"] = [p for p in data.get("positions", [])
                                     if not (p.get("market_slug") == market_slug and p.get("side") == side)]
                reg.write_text(json.dumps(data, indent=1))
            except Exception as exc:  # noqa: BLE001
                log.warning("limitless.real_registry_failed", error=str(exc)[:100])
        return out

    async def _resolve_fpmm(self, market_slug: str) -> str:
        fpmm_addr = self._fpmm_cache.get(market_slug)
        if not fpmm_addr:
            m = await self._market_client.market(market_slug)
            fpmm_addr = (m or {}).get("address")
            if not fpmm_addr:
                raise RuntimeError(f"address FPMM assente per {market_slug}")
            self._fpmm_cache[market_slug] = fpmm_addr
        return fpmm_addr

    async def fresh_quote(self, *, market_slug: str, epic: str) -> Any:
        """Quote YES fresca dal pool on-chain (prezzo eseguibile reale)."""
        from core.clock import utcnow
        from core.enums import MarketStatus
        from core.schemas import Quote

        try:
            fpmm_addr = await self._resolve_fpmm(market_slug)
        except RuntimeError:
            if self._sdk is None:
                raise
            # mercato CLOB: il prezzo eseguibile e' il book (best bid/ask), non un pool
            ob = await self._sdk.markets.get_orderbook(market_slug)
            obd = ob.model_dump() if hasattr(ob, "model_dump") else dict(ob)
            mid = float(obd.get("adjusted_midpoint") or 0.5)
            bids, asks = obd.get("bids") or [], obd.get("asks") or []
            bid = float(bids[0]["price"]) if bids else max(0.001, mid - 0.02)
            offer = float(asks[0]["price"]) if asks else min(0.999, mid + 0.02)
            return Quote(epic=epic, bid=bid, offer=offer, ts=utcnow(),
                         market_status=MarketStatus.TRADEABLE, source="limitless-clob")
        bid, offer = await asyncio.to_thread(self._price_sync, fpmm_addr)
        return Quote(epic=epic, bid=bid, offer=offer, ts=utcnow(),
                     market_status=MarketStatus.TRADEABLE, source="limitless-fpmm")

    # ------------------------------------------------------------ async API
    async def place_amm(self, *, side: str, market_slug: str, usdc_amount: float) -> dict[str, Any]:
        amount = round(min(usdc_amount, self.max_usdc_per_order), 2)
        if amount < 1.0:
            raise ValueError(f"importo {amount} sotto il minimo operativo 1 USDC")
        fpmm_addr = await self._resolve_fpmm(market_slug)
        outcome_index = 0 if side.upper() == "YES" else 1
        log.info("limitless.onchain.order", market=market_slug, side=side, usdc=amount, fpmm=fpmm_addr)
        out = await asyncio.to_thread(self._buy_sync, fpmm_addr, outcome_index, int(round(amount * 1_000_000)))
        log.info("limitless.onchain.filled", market=market_slug, side=side, usdc=amount, **{k: str(v)[:66] for k, v in out.items()})
        try:
            import json
            from pathlib import Path

            from core.clock import utcnow
            reg = Path("data/real_positions.json")
            data = json.loads(reg.read_text()) if reg.exists() else {"baseline_usdc": 0, "positions": []}
            data["positions"].append({"market_slug": market_slug, "title": market_slug[:60], "side": side,
                                      "shares": float(out.get("expectedShares") or 0), "usdc_spent": amount,
                                      "fpmm": fpmm_addr, "ts": utcnow().isoformat()})
            reg.write_text(json.dumps(data, indent=1))
        except Exception as exc:  # noqa: BLE001
            log.warning("limitless.real_registry_failed", error=str(exc)[:100])
        return out

    def _position_ids_sync(self, condition_id: str) -> list[int]:
        """positionIds derivati dal CTF (i payload CLOB non li espongono): view call gratuite."""
        from web3 import Web3
        ctf = self._w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDR), abi=CTF_MIN_ABI)
        pids = []
        for idx in (1, 2):
            col = ctf.functions.getCollectionId(b"\x00" * 32, bytes.fromhex(condition_id[2:]), idx).call()
            pids.append(ctf.functions.getPositionId(Web3.to_checksum_address(USDC), col).call())
        return pids

    async def place_fok(self, *, side: str, tokens: dict[str, Any], market_slug: str,
                        usdc_amount: float) -> dict[str, Any]:
        """BUY FOK sul CLOB (EIP-712 con l'EOA, come il maker). L'inventario finisce nel
        registro cumulativo: il redeem sweep lo incassa da solo alla risoluzione."""
        if not self.clob_enabled:
            raise RuntimeError("esecuzione CLOB non abilitata: credenziali HMAC assenti")
        from limitless_sdk.types.orders import OrderType, Side
        amount = round(min(usdc_amount, self.max_usdc_per_order), 2)
        if amount < 1.0:
            raise ValueError(f"importo {amount} sotto il minimo operativo 1 USDC")
        token_id = (tokens or {}).get(side.lower()) or (tokens or {}).get(side.upper())
        if not token_id:
            raise RuntimeError(f"token id assente per side {side}")
        resp = await self._oc.create_order(token_id=str(token_id), side=Side.BUY,
                                           order_type=OrderType.FOK, market_slug=market_slug,
                                           maker_amount=amount)
        rd = resp.model_dump(by_alias=True) if hasattr(resp, "model_dump") else dict(resp)
        status = str((rd.get("order") or {}).get("status") or "")
        log.info("limitless.clob.filled", market=market_slug, side=side, usdc=amount, status=status[:24])
        try:
            import json
            from pathlib import Path

            from collectors.limitless.markets import parse_expiry
            m = await self._market_client.market(market_slug) or {}
            cond, expiry = m.get("conditionId"), parse_expiry(m)
            if cond and expiry:
                pids = await asyncio.to_thread(self._position_ids_sync, cond)
                reg_path = Path("data/maker_touched.json")
                reg = json.loads(reg_path.read_text()) if reg_path.exists() else {}
                reg[market_slug] = {"condition_id": cond, "position_ids": [str(p) for p in pids],
                                    "expiry": expiry.isoformat()}
                reg_path.write_text(json.dumps(reg, indent=1))
        except Exception as exc:  # noqa: BLE001 - tracking best-effort: i token restano del wallet
            log.warning("limitless.clob_registry_failed", market=market_slug, error=str(exc)[:100])
        return rd

    async def aclose(self) -> None:
        if self._sdk is not None:
            try:
                await self._sdk.close()
            except Exception:  # noqa: BLE001
                pass
        return None
