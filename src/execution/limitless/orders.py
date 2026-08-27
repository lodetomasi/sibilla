"""Gateway ordini REALI su Limitless: delegated orders (firma lato server).

Nessuna chiave privata locale: autentica HMAC, il server firma per lo smart
wallet dell'account (on_behalf_of = owner id del profilo). Ordini FOK taker
con cap rigido in USDC per ordine: size e rischio restano del Risk Engine.
"""
from __future__ import annotations

from typing import Any

from core.logging import get_logger

log = get_logger("execution.limitless.orders")


class LimitlessLiveGateway:
    def __init__(self, *, api_key: str, api_secret: str, owner_id: int, max_usdc_per_order: float = 2.0):
        from limitless_sdk.sdk_client import Client
        from limitless_sdk.types.api_tokens import HMACCredentials

        self._client = Client(hmac_credentials=HMACCredentials(tokenId=api_key, secret=api_secret))
        self.owner_id = owner_id
        self.max_usdc_per_order = max_usdc_per_order

    async def place_fok(self, *, side: str, tokens: dict[str, str], market_slug: str, usdc_amount: float) -> dict[str, Any]:
        """Compra il token YES o NO spendendo `usdc_amount` USDC (FOK: o si riempie o muore).

        Ritorna un dict serializzabile con l'esito; solleva su errore API.
        """
        from limitless_sdk.types.orders import OrderType, Side

        token_id = tokens.get(side.lower())
        if not token_id:
            raise ValueError(f"token {side} assente per {market_slug}")
        amount = round(min(usdc_amount, self.max_usdc_per_order), 2)
        if amount < 1.0:
            raise ValueError(f"importo {amount} sotto il minimo operativo 1 USDC")
        log.info("limitless.live.order", market=market_slug, side=side, usdc=amount, owner=self.owner_id)
        resp = await self._client.delegated_orders.create_order(
            token_id=str(token_id),
            side=Side.BUY,               # si compra sempre il token dell'esito scelto (YES o NO)
            order_type=OrderType.FOK,
            market_slug=market_slug,
            on_behalf_of=self.owner_id,
            maker_amount=amount,         # FOK BUY: USDC da spendere
        )
        out = resp.model_dump(by_alias=True) if hasattr(resp, "model_dump") else dict(resp)
        log.info("limitless.live.filled", market=market_slug, side=side, usdc=amount,
                 matches=len(out.get("makerMatches") or []))
        return out

    async def _ensure_amm_allowance(self, slug: str) -> None:
        """Allowance USDC verso l'AMM del mercato (sponsored, una volta per mercato)."""
        from limitless_sdk.types.partner_amm import AmmAllowanceParams

        if not hasattr(self, "_amm_allowed"):
            self._amm_allowed: set[str] = set()
        if slug in self._amm_allowed:
            return
        params = AmmAllowanceParams(market=slug, side="BUY")
        check = await self._client.partner_amm.check_allowance(params)
        ok = bool(getattr(check, "confirmed", None) or (isinstance(check, dict) and check.get("confirmed")))
        if not ok:
            log.info("limitless.amm.approve", market=slug)
            await self._client.partner_amm.approve_allowance(params)
            import asyncio as _aio
            for _ in range(6):
                await _aio.sleep(5)
                check = await self._client.partner_amm.check_allowance(params)
                if bool(getattr(check, "confirmed", None) or (isinstance(check, dict) and check.get("confirmed"))):
                    ok = True
                    break
        if not ok:
            raise RuntimeError(f"allowance AMM non confermata per {slug}")
        self._amm_allowed.add(slug)

    async def place_amm(self, *, side: str, market_slug: str, usdc_amount: float) -> dict[str, Any]:
        """Compra quote YES/NO contro il pool AMM spendendo `usdc_amount` USDC."""
        import uuid as _uuid

        from limitless_sdk.types.partner_amm import AmmBuyParams

        amount = round(min(usdc_amount, self.max_usdc_per_order), 2)
        if amount < 1.0:
            raise ValueError(f"importo {amount} sotto il minimo operativo 1 USDC")
        await self._ensure_amm_allowance(market_slug)
        outcome_index = 0 if side.upper() == "YES" else 1
        params = AmmBuyParams(
            market=market_slug,
            outcomeIndex=outcome_index,
            collateralAmount=str(int(round(amount * 1_000_000))),  # USDC base units (6 decimali)
            slippageBps=300,
            idempotencyKey=_uuid.uuid4().hex,
        )
        log.info("limitless.live.amm_order", market=market_slug, side=side, usdc=amount)
        resp = await self._client.partner_amm.buy(params)
        out = resp.model_dump(by_alias=True) if hasattr(resp, "model_dump") else dict(resp)
        log.info("limitless.live.amm_filled", market=market_slug, side=side, usdc=amount,
                 status=out.get("status"), shares=out.get("expectedShares"))
        return out

    async def aclose(self) -> None:
        await self._client.close()
