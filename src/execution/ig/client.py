"""Client IG REST (patch sez. 3/22): mercati, prezzi, conto, posizioni, ordini, conferme.

Ogni chiamata passa da `IGAuthenticator` per l'ambiente scelto; il client non
conosce mai credenziali dell'altro ambiente.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from core.config import IGConfig, get_settings
from core.enums import IGEnvironment
from core.errors import UpstreamError
from core.logging import get_logger
from core.ratelimit import CircuitBreaker, TokenBucket
from execution.ig.auth import IGAuthenticator

log = get_logger("execution.ig.client")

RESOLUTIONS = {
    "SECOND",
    "MINUTE",
    "MINUTE_2",
    "MINUTE_3",
    "MINUTE_5",
    "MINUTE_10",
    "MINUTE_15",
    "MINUTE_30",
    "HOUR",
    "HOUR_2",
    "HOUR_3",
    "HOUR_4",
    "DAY",
    "WEEK",
    "MONTH",
}


class IGClient:
    """Wrapper REST minimale ma completo rispetto alla patch sez. 3."""

    def __init__(
        self,
        environment: IGEnvironment | None = None,
        config: IGConfig | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        auth: IGAuthenticator | None = None,
    ):
        settings = get_settings()
        self.environment = environment or settings.ig_environment
        self.config = config or settings.ig
        self._http = http or httpx.AsyncClient(
            base_url=self.config.base_url(self.environment), timeout=self.config.timeout_s
        )
        self._own_http = http is None
        self.auth = auth or IGAuthenticator(self.environment, self.config, http=self._http)
        self._bucket = TokenBucket(self.config.rps, burst=4)
        self._trading_bucket = TokenBucket(self.config.trading_rps, burst=2)
        self.breaker = CircuitBreaker("ig", failure_threshold=5, reset_timeout_s=30)
        self.price_allowance: dict[str, Any] = {}
        self.last_latency_ms: float = 0.0

    @property
    def configured(self) -> bool:
        return self.auth.configured

    @property
    def healthy(self) -> bool:
        return not self.breaker.is_open

    # ------------------------------------------------------------------ core
    async def authenticate(self, *, force: bool = False):
        return await self.auth.authenticate(force=force)

    async def refresh_session(self):
        return await self.auth.refresh_session()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        version: int = 1,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        trading: bool = False,
        extra_headers: dict[str, str] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        self.breaker.guard()
        await self.auth.authenticate()
        await (self._trading_bucket if trading else self._bucket).acquire()
        headers = self.auth.headers(version)
        if extra_headers:
            headers.update(extra_headers)
        started = asyncio.get_event_loop().time()
        try:
            response = await self._http.request(method, path, params=params, json=json, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self.breaker.record_failure()
            raise UpstreamError(f"IG {method} {path}: {exc}", provider="ig") from exc
        self.last_latency_ms = (asyncio.get_event_loop().time() - started) * 1000

        if response.status_code == 401 and retry_auth:
            await self.auth.refresh_session()
            return await self._request(
                method, path, version=version, params=params, json=json, trading=trading,
                extra_headers=extra_headers, retry_auth=False,
            )
        if response.status_code in (429, 500, 502, 503, 504):
            self.breaker.record_failure()
            raise UpstreamError(
                f"IG {method} {path}: HTTP {response.status_code} {_error_code(response)}",
                status_code=response.status_code,
                provider="ig",
            )
        if response.status_code >= 400:
            # errore di richiesta (es. epic inesistente): non e' un'indisponibilita
            self.breaker.record_success()
            raise UpstreamError(
                f"IG {method} {path}: HTTP {response.status_code} {_error_code(response)}",
                status_code=response.status_code,
                provider="ig",
            )
        self.breaker.record_success()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"IG {path}: risposta non JSON", provider="ig") from exc

    # -------------------------------------------------------------- mercati
    async def search_markets(self, term: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/markets", version=1, params={"searchTerm": term})
        return list(data.get("markets") or [])

    async def get_market_details(self, epic: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{epic}", version=3)

    async def get_markets(self, epics: list[str]) -> list[dict[str, Any]]:
        """Dettagli multipli (max 50 epic per chiamata)."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(epics), 50):
            chunk = epics[i : i + 50]
            data = await self._request(
                "GET", "/markets", version=2, params={"epics": ",".join(chunk), "filter": "ALL"}
            )
            out.extend(data.get("marketDetails") or [])
        return out

    async def get_prices(self, epic: str) -> dict[str, Any]:
        """Snapshot corrente (bid/offer/marketStatus) via /markets/{epic}."""
        details = await self.get_market_details(epic)
        return dict(details.get("snapshot") or {})

    async def get_historical_prices(
        self,
        epic: str,
        *,
        resolution: str = "MINUTE",
        max_points: int | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        page_size: int = 500,
    ) -> dict[str, Any]:
        if resolution not in RESOLUTIONS:
            raise ValueError(f"resolution non valida: {resolution}")
        params: dict[str, Any] = {"resolution": resolution, "pageSize": page_size, "pageNumber": 1}
        if from_ts:
            params["from"] = from_ts.strftime("%Y-%m-%dT%H:%M:%S")
        if to_ts:
            params["to"] = to_ts.strftime("%Y-%m-%dT%H:%M:%S")
        if max_points and not from_ts:
            params["max"] = max_points
        data = await self._request("GET", f"/prices/{epic}", version=3, params=params)
        allowance = (data.get("metadata") or {}).get("allowance") or {}
        if allowance:
            self.price_allowance = allowance
            if int(allowance.get("remainingAllowance", 10**6)) < self.config.price_allowance_guard:
                log.warning("ig.prices.allowance_low", remaining=allowance.get("remainingAllowance"))
        return data

    async def market_navigation(self, node_id: str | None = None) -> dict[str, Any]:
        path = "/marketnavigation" if node_id is None else f"/marketnavigation/{node_id}"
        return await self._request("GET", path, version=1)

    async def client_sentiment(self, market_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/clientsentiment/{market_id}", version=1)

    # ---------------------------------------------------------------- conto
    async def get_accounts(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/accounts", version=1)
        return list(data.get("accounts") or [])

    async def get_account(self) -> dict[str, Any]:
        session = await self.auth.authenticate()
        for account in await self.get_accounts():
            if account.get("accountId") == session.account_id:
                return account
        raise UpstreamError(f"IG: conto {session.account_id} non trovato", provider="ig")

    async def get_balance(self) -> dict[str, Any]:
        return dict((await self.get_account()).get("balance") or {})

    async def get_margin(self) -> float:
        return float((await self.get_balance()).get("deposit") or 0.0)

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/positions", version=2)
        return list(data.get("positions") or [])

    async def get_position(self, deal_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/positions/{deal_id}", version=2)

    async def get_working_orders(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/workingorders", version=2)
        return list(data.get("workingOrders") or [])

    async def get_activity(self, *, from_ts: datetime, detailed: bool = True) -> list[dict[str, Any]]:
        params = {"from": from_ts.strftime("%Y-%m-%dT%H:%M:%S"), "detailed": str(detailed).lower(), "pageSize": 500}
        data = await self._request("GET", "/history/activity", version=3, params=params)
        return list(data.get("activities") or [])

    async def get_transactions(self, *, from_ts: datetime) -> list[dict[str, Any]]:
        params = {"from": from_ts.strftime("%Y-%m-%dT%H:%M:%S"), "type": "ALL", "pageSize": 500}
        data = await self._request("GET", "/history/transactions", version=2, params=params)
        return list(data.get("transactions") or [])

    # --------------------------------------------------------------- dealing
    async def create_position(self, payload: dict[str, Any]) -> str:
        """POST /positions/otc v2 -> dealReference (poi SEMPRE get_confirm, patch sez. 24)."""
        data = await self._request("POST", "/positions/otc", version=2, json=payload, trading=True)
        reference = data.get("dealReference")
        if not reference:
            raise UpstreamError("IG create_position: dealReference mancante", provider="ig")
        return str(reference)

    async def close_position(self, payload: dict[str, Any]) -> str:
        """DELETE /positions/otc v1 (via POST + _method override, come da API IG)."""
        data = await self._request(
            "POST",
            "/positions/otc",
            version=1,
            json=payload,
            trading=True,
            extra_headers={"_method": "DELETE"},
        )
        reference = data.get("dealReference")
        if not reference:
            raise UpstreamError("IG close_position: dealReference mancante", provider="ig")
        return str(reference)

    async def update_position(self, deal_id: str, payload: dict[str, Any]) -> str:
        data = await self._request(
            "PUT", f"/positions/otc/{deal_id}", version=2, json=payload, trading=True
        )
        return str(data.get("dealReference") or "")

    async def create_working_order(self, payload: dict[str, Any]) -> str:
        data = await self._request("POST", "/workingorders/otc", version=2, json=payload, trading=True)
        return str(data.get("dealReference") or "")

    async def delete_working_order(self, deal_id: str) -> str:
        data = await self._request(
            "POST",
            f"/workingorders/otc/{deal_id}",
            version=2,
            json={},
            trading=True,
            extra_headers={"_method": "DELETE"},
        )
        return str(data.get("dealReference") or "")

    async def get_confirm(self, deal_reference: str) -> dict[str, Any]:
        return await self._request("GET", f"/confirms/{deal_reference}", version=1)

    async def aclose(self) -> None:
        await self.auth.logout()
        if self._own_http:
            await self._http.aclose()


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("errorCode", ""))
    except Exception:  # noqa: BLE001
        return response.text[:160]
