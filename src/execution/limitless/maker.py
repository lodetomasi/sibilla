"""Maker v1 su CLOB Limitless: cattura del set completo.

Posta bid post-only su YES e NO con somma <= target (default 0.955). Quando
entrambe vengono riempite dai venditori, il set completo paga 1.00 a risoluzione
(redeem CTF on-chain): profitto strutturale ~4.5% al netto del rischio di fill
singolo (esposizione max 1 lato per pochi minuti sui mercati brevi).

Caps rigidi: mercati concorrenti, USDC impegnati, stop se saldo scende.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from core.clock import utcnow
from core.logging import get_logger

log = get_logger("execution.limitless.maker")

CTF = "0xC9c98965297Bc527861c898329Ee280632B76e18"  # Conditional Tokens (Base)
CTF_ABI = [
    {"name": "redeemPositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "collateralToken", "type": "address"}, {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"}, {"name": "indexSets", "type": "uint256[]"}],
     "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "payoutDenominator", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "conditionId", "type": "bytes32"}], "outputs": [{"type": "uint256"}]},
]


def pair_bids(mid_yes: float, pair_target: float = 0.955) -> tuple[float, float]:
    """Bid YES e NO proporzionali al mid, con somma = pair_target (tick 0.001)."""
    mid_yes = min(0.95, max(0.05, mid_yes))
    b_yes = round(mid_yes * pair_target, 3)
    b_no = round(pair_target - b_yes, 3)
    return max(0.002, b_yes), max(0.002, b_no)


def completion_cap(paid: float, cap_total: float = 0.99) -> float:
    """Prezzo massimo per completare il set quando la gamba riempita e' costata `paid`.

    Sotto questo prezzo il set completo costa <= cap_total e paga 1.00 a risoluzione:
    profitto bloccato, non scommessa. Sopra: meglio nessun inseguimento."""
    return round(max(0.0, cap_total - paid), 3)


# ticker Binance per gli hourly up/down: il fair value viene dal sottostante, non dal book
_BINANCE = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT",
            "doge": "DOGEUSDT", "bnb": "BNBUSDT", "gold-paxg": "PAXGUSDT"}


def digital_p_up(spot: float, open_price: float, sigma_1m: float, seconds_left: float) -> float:
    """P(spot a scadenza > open) per un'opzione digitale senza drift.

    Phi( ln(S/K) / (sigma_1m * sqrt(minuti residui)) ): e' il fair del mercato up/down.
    Quotare attorno a QUESTO valore — mai al mid del book (placeholder/stale) — e' cio'
    che separa un maker informato da cibo per taker."""
    import math
    if spot <= 0 or open_price <= 0:
        return 0.5
    vol = max(sigma_1m, 1e-5) * math.sqrt(max(seconds_left, 30.0) / 60.0)
    z = math.log(spot / open_price) / vol
    return min(0.99, max(0.01, 0.5 * (1 + math.erf(z / math.sqrt(2)))))


class CompleteSetMaker:
    def __init__(self, *, api_key: str, api_secret: str, private_key: str,
                 pair_target: float = 0.955, size_usdc: float = 2.0,
                 max_markets: int = 1, min_seconds_left: float = 600.0,
                 rpc: str = "https://base-rpc.publicnode.com"):
        from eth_account import Account
        from web3 import Web3

        from execution.limitless.client import LimitlessClient
        from limitless_sdk.orders import OrderClient
        from limitless_sdk.sdk_client import Client
        from limitless_sdk.types.api_tokens import HMACCredentials

        self._sdk = Client(hmac_credentials=HMACCredentials(tokenId=api_key, secret=api_secret))
        self._acct = Account.from_key(private_key)
        self.address = self._acct.address
        self._oc = OrderClient(self._sdk.http, self._acct, market_fetcher=self._sdk.markets)
        self._lc = LimitlessClient()
        self._w3 = None
        for _rpc in (rpc, "https://base-rpc.publicnode.com", "https://mainnet.base.org", "https://base.drpc.org"):
            try:
                _w3 = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 8}))
                _w3.eth.block_number  # probe
                self._w3 = _w3
                log.info("maker.rpc", endpoint=_rpc)
                break
            except Exception:  # noqa: BLE001
                continue
        if self._w3 is None:
            raise RuntimeError("nessun RPC Base raggiungibile per il maker")
        # endpoint dedicato alle TRANSAZIONI (publicnode & co. rifiutano i broadcast con 403)
        self._w3_tx = None
        for _rpc in ("https://mainnet.base.org", "https://base.drpc.org"):
            try:
                _w = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 15}))
                _w.eth.block_number
                self._w3_tx = _w
                break
            except Exception:  # noqa: BLE001
                continue
        if self._w3_tx is None:
            self._w3_tx = self._w3
        self._ctf = self._w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)
        self.pair_target = pair_target
        self.size_usdc = size_usdc
        self.max_markets = max_markets
        self.min_seconds_left = min_seconds_left
        # stato per mercato: {slug: {"expiry": dt, "condition_id": str, "position_ids": [..], "orders": [ids]}}
        self._live: dict[str, dict[str, Any]] = {}
        self._pending_redeem: dict[str, dict[str, Any]] = {}
        self._nonce_lock = asyncio.Lock()

    async def _fair_p_up(self, m: dict[str, Any]) -> float | None:
        """Fair value dell'hourly dal sottostante (spot Binance + vol 1m, cache 10min)."""
        sym = next((v for k, v in _BINANCE.items() if m["slug"].startswith(k + "-")), None)
        open_price = float(m.get("open_price") or 0)
        if not sym or open_price <= 0:
            return None
        import math

        import httpx
        now = utcnow().timestamp()
        if not hasattr(self, "_volcache"):
            self._volcache: dict[str, tuple[float, float]] = {}
        async with httpx.AsyncClient(timeout=8) as cli:
            cached = self._volcache.get(sym)
            if not cached or now - cached[1] > 600:
                kl = (await cli.get("https://api.binance.com/api/v3/klines",
                                    params={"symbol": sym, "interval": "1m", "limit": 60})).json()
                closes = [float(k[4]) for k in kl]
                rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1) if closes[i] > 0]
                sigma = (sum(r * r for r in rets) / len(rets)) ** 0.5 if rets else 0.001
                self._volcache[sym] = (sigma, now)
            spot = float((await cli.get("https://api.binance.com/api/v3/ticker/price",
                                        params={"symbol": sym})).json()["price"])
        return digital_p_up(spot, open_price, self._volcache[sym][0],
                            (m["expiry"] - utcnow()).total_seconds())

    def _position_ids_sync(self, condition_id: str) -> list[int]:
        """Deriva i positionIds dal CTF (i payload CLOB non li espongono): view call gratuite."""
        from web3 import Web3

        from execution.limitless.onchain import USDC
        abi = [
            {"name": "getCollectionId", "type": "function", "stateMutability": "view",
             "inputs": [{"name": "p", "type": "bytes32"}, {"name": "c", "type": "bytes32"}, {"name": "i", "type": "uint256"}],
             "outputs": [{"type": "bytes32"}]},
            {"name": "getPositionId", "type": "function", "stateMutability": "pure",
             "inputs": [{"name": "col", "type": "address"}, {"name": "collection", "type": "bytes32"}],
             "outputs": [{"type": "uint256"}]},
        ]
        ctf = self._w3.eth.contract(address=self._ctf.address, abi=abi)
        pids = []
        for idx in (1, 2):
            col = ctf.functions.getCollectionId(b"\x00" * 32, bytes.fromhex(condition_id[2:]), idx).call()
            pids.append(ctf.functions.getPositionId(Web3.to_checksum_address(USDC), col).call())
        return pids

    # ------------------------------------------------------------- discovery
    async def _clob_markets(self) -> list[dict[str, Any]]:
        from collectors.limitless.markets import parse_expiry

        mk = await self._lc.active_markets(limit=25, page=1)
        out = []
        for m in mk:
            if m.get("tradeType") != "clob" or not m.get("tokens"):
                continue
            expiry = parse_expiry(m)
            if expiry is None:
                continue
            left = (expiry - utcnow()).total_seconds()
            if left < self.min_seconds_left:
                continue
            settings = m.get("settings") or {}
            if float(settings.get("rebateRate") or 0) < 1:
                continue  # solo mercati maker-rebate pieno (hourly crypto): book piu' vivi, ciclo 1h
            if "hourly" not in m["slug"]:
                continue  # velocita' del capitale: ogni ora il set torna cash (i daily lo bloccano 24h)
            out.append({"slug": m["slug"], "tokens": m["tokens"], "expiry": expiry,
                        "condition_id": m.get("conditionId"), "position_ids": m.get("positionIds") or [],
                        "open_price": float((m.get("metadata") or {}).get("openPrice") or 0),
                        "volume": float(m.get("volume") or 0), "seconds_left": left})
        # i piu' scambiati per primi: la set-completion richiede un book con controparti vive
        return sorted(out, key=lambda x: -x["volume"])

    async def _boot_cleanup(self) -> None:
        """Al primo tick dopo un riavvio: cancella gli ordini rimasti sul book dai giri precedenti."""
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        try:
            import json
            from pathlib import Path
            old = json.loads(Path("data/maker_state.json").read_text())
        except Exception:  # noqa: BLE001
            return
        for slug in old:
            if slug in self._live:
                continue
            try:
                await self._oc.cancel_all(slug)
                log.info("maker.boot_cleanup", market=slug[:40])
            except Exception as exc:  # noqa: BLE001
                log.debug("maker.boot_cleanup_failed", market=slug[:40], error=str(exc)[:80])

    async def _news_veto(self, m: dict[str, Any]) -> bool:
        """Veto AI (flash, ~600s di cache): quotare questo mercato ORA e' tossico per un maker passivo?

        L'AI non tocca prezzi ne' size: alza solo la mano su event-risk (news programmate, volatilita' in corso).
        Fail-open: se il modello non risponde, si continua a quotare (il rischio resta bounded dai cap)."""
        from datetime import timedelta
        slug = m["slug"]
        until = getattr(self, "_veto_until", {}).get(slug)
        if until and utcnow() < until:
            return True
        checked = getattr(self, "_veto_checked", {}).get(slug)
        if checked and (utcnow() - checked).total_seconds() < 600:
            return False
        if not hasattr(self, "_veto_until"):
            self._veto_until, self._veto_checked = {}, {}
        self._veto_checked[slug] = utcnow()
        try:
            from pydantic import BaseModel

            from intelligence.llm import get_llm_client

            class MakerVeto(BaseModel):
                toxic: bool
                reason: str = ""

            prompt = ("Sei il risk officer di un market maker passivo su mercati binari a breve scadenza. "
                      f"Mercato: '{slug.replace('-', ' ')}', scade {m['expiry'].isoformat()} (ora: {utcnow().isoformat()}). "
                      "toxic=true SOLO se c'e' un evento programmato imminente o volatilita' eccezionale in corso "
                      "che rende tossico quotare passivamente ADESSO (es. dato macro/Fed a minuti, crash in corso). "
                      "Il normale rumore di mercato NON e' tossico.")
            res = await get_llm_client().complete("high_volume_filter", [{"role": "user", "content": prompt}], schema=MakerVeto)
            if res.parsed and res.parsed.toxic:
                self._veto_until[slug] = utcnow() + timedelta(minutes=30)
                log.info("maker.veto", market=slug[:40], reason=res.parsed.reason[:120])
                return True
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.debug("maker.veto_failed", market=slug[:40], error=str(exc)[:80])
        return False

    # ------------------------------------------------------------------ tick
    async def tick(self) -> dict[str, Any]:
        posted = 0
        await self._boot_cleanup()
        # 0) PRIMA SI INCASSA: redeem dei set maturati (mai in coda dietro veto/quote)
        redeemed = await self._redeem_ready()
        # 1) scadenze: cancella quote sui mercati vicini alla chiusura, sposta in redeem
        for slug, st in list(self._live.items()):
            left = (st["expiry"] - utcnow()).total_seconds()
            if left < 60:
                try:
                    await self._oc.cancel_all(slug)
                except Exception as exc:  # noqa: BLE001
                    log.debug("maker.cancel_all_failed", market=slug, error=str(exc)[:80])
                self._pending_redeem[slug] = st
                del self._live[slug]

        # 1a-bis) set-completion: gambe singole gia' riempite -> blocca il profitto
        completed = await self._complete_singles()

        # 1b) veto AI sui mercati in quotazione: se l'ambiente diventa tossico, ritira le quote
        for slug, st in list(self._live.items()):
            if await self._news_veto(st):
                try:
                    await self._oc.cancel_all(slug)
                except Exception:  # noqa: BLE001
                    pass
                self._pending_redeem[slug] = st
                del self._live[slug]

        # 1c) reprice su drift: quote ferme mentre il fair si muove = stale quotes da manuale
        for slug, st in list(self._live.items()):
            try:
                fair_now = await self._fair_p_up(st)
            except Exception:  # noqa: BLE001 - spot momentaneamente ko: tieni le quote
                continue
            if fair_now is None or abs(fair_now - st.get("fair", fair_now)) <= 0.04:
                continue
            try:
                await self._oc.cancel_all(slug)
            except Exception:  # noqa: BLE001
                pass
            del self._live[slug]  # rientra subito sotto con le quote sul fair aggiornato
            log.info("maker.reprice", market=slug[:40], fair=round(fair_now, 3), was=round(st.get("fair", 0), 3))

        # 2) nuove quote
        if len(self._live) < self.max_markets:
            markets = await self._clob_markets()
            for m in markets:
                if len(self._live) >= self.max_markets:
                    break
                if m["slug"] in self._live or m["slug"] in self._pending_redeem:
                    continue
                if False and await self._news_veto(m):
                    continue
                try:
                    posted += await self._quote_market(m)
                except Exception as exc:  # noqa: BLE001
                    log.warning("maker.quote_failed", market=m["slug"][:40], error=str(exc)[:120])

        redeemed_late = 0  # redeem gia' eseguito a inizio tick
        try:
            import json
            from pathlib import Path
            state = {slug: {"position_ids": st.get("position_ids") or [], "expiry": st["expiry"].isoformat()}
                     for slug, st in {**self._live, **self._pending_redeem}.items()}
            Path("data/maker_state.json").write_text(json.dumps(state))
        except Exception as exc:  # noqa: BLE001
            log.debug("maker.state_dump_failed", error=str(exc)[:80])
        return {"quoting": len(self._live), "posted": posted, "completed": completed, "pending_redeem": len(self._pending_redeem), "redeemed": redeemed}

    async def _quote_market(self, m: dict[str, Any]) -> int:
        from limitless_sdk.types.orders import OrderType, Side

        # positionIds/conditionId vivono solo nel payload del singolo mercato: fetch (necessario al redeem)
        if not m.get("position_ids"):
            full = await self._lc.market(m["slug"])
            m["position_ids"] = (full or {}).get("positionIds") or []
            m["condition_id"] = m.get("condition_id") or (full or {}).get("conditionId")
        if not m.get("position_ids"):
            # riusa pids dal registro touched (evita ricalcoli on-chain: anti-429)
            try:
                import json as _json
                from pathlib import Path as _Path
                _reg = _json.loads(_Path("data/maker_touched.json").read_text())
                _pids = (_reg.get(m["slug"]) or {}).get("position_ids") or []
                if _pids:
                    m["position_ids"] = [int(x) for x in _pids]
            except Exception:  # noqa: BLE001
                pass
        if not m.get("position_ids") and m.get("condition_id"):
            m["position_ids"] = await asyncio.to_thread(self._position_ids_sync, m["condition_id"])

        # idempotenza: al (ri)avvio o re-quote, azzera eventuali ordini nostri gia' sul book
        try:
            await self._oc.cancel_all(m["slug"])
        except Exception:  # noqa: BLE001 - nessun ordine da cancellare va bene
            pass
        # memoria di mercato: la baseline si misura UNA volta (primo giro di quote), poi
        # sopravvive ai requote — cosi' le gambe riempite nei giri precedenti restano
        # completabili invece di diventare "ereditate" a ogni reprice. Il costo presunto
        # della gamba riempita e' la PEGGIORE (piu' alta) delle nostre bid dell'ora: conservativo.
        if not hasattr(self, "_mkt_mem"):
            self._mkt_mem: dict[str, dict[str, float]] = {}
        rec = self._mkt_mem.get(m["slug"])
        if rec is None:
            base_yes = base_no = 0.0
            try:
                if len(m.get("position_ids") or []) >= 2:
                    b = [await asyncio.to_thread(self._ctf.functions.balanceOf(self.address, int(p)).call)
                         for p in m["position_ids"][:2]]
                    base_yes, base_no = b[0] / 1e6, b[1] / 1e6
            except Exception as exc:  # noqa: BLE001 - RPC ko: baseline 0 e' conservativa solo se inventario 0
                log.debug("maker.baseline_failed", market=m["slug"][:40], error=str(exc)[:80])
            rec = self._mkt_mem[m["slug"]] = {"base_yes": base_yes, "base_no": base_no,
                                              "max_bid_yes": 0.0, "max_bid_no": 0.0}
        if len(self._mkt_mem) > 50:  # prova a non crescere per sempre
            for slug in [s for s in self._mkt_mem if s not in self._live and s not in self._pending_redeem][:-10]:
                self._mkt_mem.pop(slug, None)
        # tetto d'inventario CUMULATIVO per mercato: ogni requote ripiazza GTC 2+2 e il book
        # puo' colpirle tutte -> senza tetto l'esposizione oraria e' size x N requote, non size.
        # Oltre il tetto: niente nuove quote, restano completion (tick) e redeem.
        if rec.get("inv", 0.0) >= 4 * self.size_usdc:
            self._live[m["slug"]] = {**m, "orders": [], "fair": rec.get("fair", 0.5),
                                     "bid_yes": rec["max_bid_yes"] or 0.5, "bid_no": rec["max_bid_no"] or 0.5,
                                     "base_yes": rec["base_yes"], "base_no": rec["base_no"]}
            log.info("maker.inventory_cap", market=m["slug"][:40], inv=round(rec["inv"], 2))
            return 0
        fair = await self._fair_p_up(m)
        if fair is None:
            log.debug("maker.no_fair", market=m["slug"][:40])
            return 0  # senza fair dal sottostante NON si quota: il mid del book puo' essere finto
        b_yes, b_no = pair_bids(fair, self.pair_target)
        orders = []
        try:
            for side_name, token_key, price in (("YES", "yes", b_yes), ("NO", "no", b_no)):
                resp = await self._oc.create_order(token_id=str(m["tokens"][token_key]), side=Side.BUY,
                                                   order_type=OrderType.GTC, market_slug=m["slug"],
                                                   price=price, size=self.size_usdc, post_only=True)
                rd = resp.model_dump(by_alias=True) if hasattr(resp, "model_dump") else dict(resp)
                orders.append((rd.get("order") or {}).get("id"))
                log.info("maker.quote", market=m["slug"][:40], side=side_name, price=price, size=self.size_usdc)
        except Exception:
            # rollback: mai lasciare una gamba sola sul book (esposizione direzionale non voluta)
            try:
                await self._oc.cancel_all(m["slug"])
            except Exception:  # noqa: BLE001
                pass
            raise
        rec["max_bid_yes"] = max(rec["max_bid_yes"], b_yes)
        rec["max_bid_no"] = max(rec["max_bid_no"], b_no)
        self._live[m["slug"]] = {**m, "orders": orders, "fair": fair,
                                 "bid_yes": rec["max_bid_yes"], "bid_no": rec["max_bid_no"],
                                 "base_yes": rec["base_yes"], "base_no": rec["base_no"]}
        self._touch_registry(m)
        return len(orders)

    async def _complete_singles(self) -> int:
        """Gamba singola riempita? Completa il set con FAK solo a costo totale <= 0.99.

        Trasforma l'adverse selection (scommessa direzionale subita) in profitto bloccato:
        se il FAK non incrocia entro il cap, nessun inseguimento — si ritenta al tick dopo."""
        from limitless_sdk.types.orders import OrderType, Side
        done = 0
        for slug, st in list(self._live.items()):
            pids = st.get("position_ids") or []
            if len(pids) < 2:
                continue
            try:
                bal = [await asyncio.to_thread(self._ctf.functions.balanceOf(self.address, int(p)).call)
                       for p in pids[:2]]
            except Exception as exc:  # noqa: BLE001 - RPC ko: riprova al tick dopo
                log.debug("maker.balance_check_failed", market=slug[:40], error=str(exc)[:80])
                continue
            # aggiorna l'inventario cumulativo visto (per il tetto anti-churn in _quote_market)
            if hasattr(self, "_mkt_mem") and slug in self._mkt_mem:
                self._mkt_mem[slug]["inv"] = bal[0] / 1e6 + bal[1] / 1e6
                self._mkt_mem[slug]["fair"] = st.get("fair", 0.5)
            # solo il delta rispetto alla baseline pre-quota: e' l'unico inventario di cui
            # conosciamo il costo (le nostre bid); il resto va al redeem, mai inseguito
            diff = (bal[0] / 1e6 - st.get("base_yes", 0.0)) - (bal[1] / 1e6 - st.get("base_no", 0.0))
            if abs(diff) < 0.2:
                continue
            missing = "no" if diff > 0 else "yes"
            paid = st.get("bid_yes" if diff > 0 else "bid_no")
            if paid is None:
                continue
            cap = completion_cap(paid)
            if cap <= 0.002:
                continue
            size = round(abs(diff), 2)
            try:
                resp = await self._oc.create_order(token_id=str(st["tokens"][missing]), side=Side.BUY,
                                                   order_type=OrderType.FAK, market_slug=slug,
                                                   price=cap, size=size)
                rd = resp.model_dump(by_alias=True) if hasattr(resp, "model_dump") else dict(resp)
                status = str((rd.get("order") or {}).get("status") or "")
                if "FILL" in status.upper():  # solo i fill sono notizie; i KILLED ritentano in silenzio
                    log.info("maker.set_completion", market=slug[:40], side=missing.upper(),
                             price=cap, size=size, status=status)
                    done += 1
                else:
                    log.debug("maker.completion_pending", market=slug[:40], status=status, price=cap)
            except Exception as exc:  # noqa: BLE001 - book fuori cap: normale, si ritenta
                log.debug("maker.completion_skipped", market=slug[:40], error=str(exc)[:100])
        return done

    def _touch_registry(self, m: dict[str, Any]) -> None:
        """Registro cumulativo dei mercati toccati: sopravvive a rotazioni e riavvii."""
        import json
        from pathlib import Path
        reg_path = Path("data/maker_touched.json")
        try:
            reg = json.loads(reg_path.read_text()) if reg_path.exists() else {}
        except Exception:  # noqa: BLE001
            reg = {}
        reg[m["slug"]] = {"condition_id": m.get("condition_id"),
                          "position_ids": [str(p) for p in (m.get("position_ids") or [])],
                          "expiry": m["expiry"].isoformat()}
        reg_path.write_text(json.dumps(reg, indent=1))

    # ---------------------------------------------------------------- redeem
    def _redeem_sync(self, condition_id: str) -> str | None:
        from web3 import Web3

        from execution.limitless.onchain import USDC

        if self._ctf.functions.payoutDenominator(bytes.fromhex(condition_id[2:])).call() == 0:
            return None  # non ancora risolto
        w3 = self._w3_tx  # broadcast SOLO su endpoint che accettano transazioni
        ctf = w3.eth.contract(address=self._ctf.address, abi=CTF_ABI)
        fn = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC), b"\x00" * 32, bytes.fromhex(condition_id[2:]), [1, 2])
        chain_nonce = w3.eth.get_transaction_count(self.address, "pending")
        nonce = max(chain_nonce, getattr(self, "_next_nonce", 0))
        tx = fn.build_transaction({"from": self.address, "nonce": nonce,
                                   "maxFeePerGas": w3.eth.gas_price * 2,
                                   "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"), "chainId": 8453})
        signed = self._acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        self._next_nonce = nonce + 1
        return h.hex() if receipt.status == 1 else None

    async def _redeem_ready(self) -> int:
        import json
        from pathlib import Path
        done = 0
        reg_path = Path("data/maker_touched.json")
        try:
            registry = json.loads(reg_path.read_text()) if reg_path.exists() else {}
        except Exception:  # noqa: BLE001
            registry = {}
        merged: dict[str, dict[str, Any]] = {}
        for slug, st in registry.items():
            from datetime import datetime
            merged[slug] = {"condition_id": st.get("condition_id"),
                            "position_ids": st.get("position_ids") or [],
                            "expiry": datetime.fromisoformat(st["expiry"])}
        merged.update(self._pending_redeem)
        dirty = False
        for slug, st in list(merged.items()):
            if (utcnow() - st["expiry"]).total_seconds() < 90:
                continue
            # abbiamo token da riscattare?
            has_balance = False
            check_failed = False
            for pid in (st.get("position_ids") or [])[:2]:
                try:
                    if self._ctf.functions.balanceOf(self.address, int(pid)).call() > 0:
                        has_balance = True
                        break
                except Exception:  # noqa: BLE001 - RPC ko: NON cancellare, riprova al giro dopo
                    check_failed = True
                    break
            if check_failed:
                continue
            if not has_balance:
                self._pending_redeem.pop(slug, None)
                if slug in registry:
                    del registry[slug]
                    dirty = True
                continue
            try:
                async with self._nonce_lock:
                    txh = await asyncio.to_thread(self._redeem_sync, st["condition_id"])
                if txh:
                    log.info("maker.redeemed", market=slug[:40], tx=txh[:18])
                    self._pending_redeem.pop(slug, None)
                    if slug in registry:
                        del registry[slug]
                        dirty = True
                    done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("maker.redeem_failed", market=slug[:40], error=str(exc)[:120])
        if dirty:
            try:
                reg_path.write_text(json.dumps(registry, indent=1))
            except Exception:  # noqa: BLE001
                pass
        return done

    async def aclose(self) -> None:
        try:
            await self._sdk.close()
        finally:
            await self._lc.aclose()
