"""Client Polymarket: Gamma (metadati), CLOB (book/prezzi), Data API (wallet).

Sez. 4.1: markets, metadata, outcome, prezzi YES/NO, order book, bid/ask, spread,
liquidity, volume, historical prices, trades, wallet activity/positions/PnL,
resolution, category, date. Nessuna API key richiesta per la parte read-only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.config import PolymarketConfig, get_settings
from core.http import HttpClient
from core.logging import get_logger

log = get_logger("collectors.polymarket.client")


class PolymarketClient:
    """Wrapper unico sui tre endpoint pubblici di Polymarket."""

    def __init__(
        self,
        config: PolymarketConfig | None = None,
        *,
        gamma: HttpClient | None = None,
        clob: HttpClient | None = None,
        data: HttpClient | None = None,
    ):
        self.config = config or get_settings().polymarket
        rps = self.config.rps
        timeout = self.config.timeout_s
        proxy = self.config.proxy  # uscita fuori-Italia opzionale (Tor/VPN), fallback diretto+DoH
        self.gamma = gamma or HttpClient(
            self.config.gamma_url, provider="polymarket-gamma", rps=rps, timeout_s=timeout, proxy=proxy
        )
        self.clob = clob or HttpClient(
            self.config.clob_url, provider="polymarket-clob", rps=rps, timeout_s=timeout, proxy=proxy
        )
        self.data = data or HttpClient(
            self.config.data_url, provider="polymarket-data", rps=rps, timeout_s=timeout, proxy=proxy
        )
        if proxy:
            log.info("polymarket.proxy.enabled", proxy=proxy)

    # ------------------------------------------------------------------ Gamma
    async def list_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        active: bool | None = True,
        closed: bool | None = False,
        order: str = "volume24hr",
        ascending: bool = False,
        tag: str | None = None,
        start_date_min: datetime | None = None,
        end_date_min: datetime | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "order": order,
                                  "ascending": str(ascending).lower()}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if tag:
            params["tag"] = tag
        if start_date_min:
            params["start_date_min"] = start_date_min.isoformat()
        if end_date_min:
            params["end_date_min"] = end_date_min.isoformat()
        payload = await self.gamma.get_json("/markets", params=params)
        return _as_list(payload, "markets")

    async def iter_markets(
        self, *, page_size: int = 100, max_pages: int = 50, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Paginazione completa (historical batch)."""
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = await self.list_markets(limit=page_size, offset=page * page_size, **kwargs)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    async def get_market(self, market_id: str) -> dict[str, Any] | None:
        try:
            payload = await self.gamma.get_json(f"/markets/{market_id}")
        except Exception as exc:  # noqa: BLE001
            log.warning("polymarket.market.not_found", market_id=market_id, error=str(exc)[:120])
            return None
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload

    async def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        payload = await self.gamma.get_json("/markets", params={"slug": slug})
        items = _as_list(payload, "markets")
        return items[0] if items else None

    async def list_events(
        self, *, limit: int = 100, offset: int = 0, closed: bool | None = False
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        payload = await self.gamma.get_json("/events", params=params)
        return _as_list(payload, "events")

    # ------------------------------------------------------------------- CLOB
    async def get_book(self, token_id: str) -> dict[str, Any]:
        return await self.clob.get_json("/book", params={"token_id": token_id})

    async def get_books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        if not token_ids:
            return []
        payload = await self.clob.post_json(
            "/books", json=[{"token_id": t} for t in token_ids]
        )
        return payload if isinstance(payload, list) else [payload]

    async def get_price(self, token_id: str, side: str = "buy") -> float | None:
        payload = await self.clob.get_json(
            "/price", params={"token_id": token_id, "side": side.lower()}
        )
        value = payload.get("price") if isinstance(payload, dict) else None
        return float(value) if value is not None else None

    async def get_midpoint(self, token_id: str) -> float | None:
        payload = await self.clob.get_json("/midpoint", params={"token_id": token_id})
        value = payload.get("mid") if isinstance(payload, dict) else None
        return float(value) if value is not None else None

    async def get_spread(self, token_id: str) -> float | None:
        payload = await self.clob.get_json("/spread", params={"token_id": token_id})
        value = payload.get("spread") if isinstance(payload, dict) else None
        return float(value) if value is not None else None

    async def price_history(
        self,
        token_id: str,
        *,
        interval: str | None = "1d",
        start_ts: int | None = None,
        end_ts: int | None = None,
        fidelity: int | None = None,
    ) -> list[dict[str, Any]]:
        """Storico prezzi CLOB (`/prices-history`). fidelity = minuti per punto."""
        params: dict[str, Any] = {"market": token_id}
        if start_ts and end_ts:
            params["startTs"] = start_ts
            params["endTs"] = end_ts
        elif interval:
            params["interval"] = interval
        if fidelity:
            params["fidelity"] = fidelity
        payload = await self.clob.get_json("/prices-history", params=params)
        if isinstance(payload, dict):
            return payload.get("history", []) or []
        return payload or []

    async def market_trades(self, condition_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Trade recenti su un mercato (via Data API, che non richiede auth)."""
        payload = await self.data.get_json(
            "/trades", params={"market": condition_id, "limit": limit}
        )
        return _as_list(payload, "trades")

    # --------------------------------------------------------------- Data API
    async def wallet_positions(
        self, address: str, *, limit: int = 500, size_threshold: float = 0.1
    ) -> list[dict[str, Any]]:
        payload = await self.data.get_json(
            "/positions",
            params={"user": address, "limit": limit, "sizeThreshold": size_threshold},
        )
        return _as_list(payload, "positions")

    async def wallet_trades(
        self, address: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        payload = await self.data.get_json(
            "/trades", params={"user": address, "limit": limit, "offset": offset}
        )
        return _as_list(payload, "trades")

    async def wallet_activity(
        self, address: str, *, limit: int = 500, offset: int = 0, kind: str | None = "TRADE"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": address, "limit": limit, "offset": offset}
        if kind:
            params["type"] = kind
        payload = await self.data.get_json("/activity", params=params)
        return _as_list(payload, "activity")

    async def wallet_value(self, address: str) -> float | None:
        payload = await self.data.get_json("/value", params={"user": address})
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if isinstance(payload, dict):
            for key in ("value", "user_value", "total"):
                if key in payload:
                    return float(payload[key])
        return None

    async def market_holders(
        self, condition_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Wallet con posizione aperta su un mercato: seme per la discovery."""
        payload = await self.data.get_json(
            "/holders", params={"market": condition_id, "limit": limit}
        )
        if isinstance(payload, dict):
            holders: list[dict[str, Any]] = []
            for group in payload.get("holders", []) or []:
                if isinstance(group, dict) and "holders" in group:
                    holders.extend(group.get("holders") or [])
                else:
                    holders.append(group)
            return holders
        return _as_list(payload, "holders")

    async def leaderboard(
        self, *, window: str = "all", kind: str = "pnl", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Leaderboard wallet. L'endpoint pubblico cambia spesso: fallback vuoto."""
        for path, params in (
            ("/leaderboard", {"window": window, "orderBy": kind, "limit": limit}),
            ("/rankings", {"window": window, "type": kind, "limit": limit}),
        ):
            try:
                payload = await self.data.get_json(path, params=params)
                items = _as_list(payload, "leaderboard")
                if items:
                    return items
            except Exception as exc:  # noqa: BLE001
                log.info("polymarket.leaderboard.unavailable", path=path, error=str(exc)[:120])
        return []

    @property
    def healthy(self) -> bool:
        return self.gamma.healthy and self.clob.healthy and self.data.healthy

    def stats(self) -> dict[str, Any]:
        return {
            "gamma": self.gamma.stats.snapshot(),
            "clob": self.clob.stats.snapshot(),
            "data": self.data.stats.snapshot(),
        }

    async def aclose(self) -> None:
        for client in (self.gamma, self.clob, self.data):
            await client.aclose()


def _as_list(payload: Any, key: str) -> list[dict[str, Any]]:
    """Le API Polymarket a volte rispondono con lista, a volte con {key: [...]}"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for candidate in (key, "data", "results", "items"):
            value = payload.get(candidate)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload] if payload else []
    return []
