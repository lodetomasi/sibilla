"""Autenticazione IG (patch sez. 23).

- API key, session token (CST + X-SECURITY-TOKEN), scadenza/refresh, account ID;
- DEMO e LIVE hanno credenziali e base URL separati e mai condivisi;
- nessun secret nei log (sez. 52).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.clock import utcnow
from core.config import IGConfig, IGCredentials, get_settings
from core.enums import IGEnvironment
from core.errors import ConfigError, UpstreamError
from core.logging import get_logger

log = get_logger("execution.ig.auth")


@dataclass
class IGSession:
    environment: IGEnvironment
    cst: str
    security_token: str
    account_id: str
    lightstreamer_endpoint: str
    currency: str = "EUR"
    account_type: str = ""
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime = field(default_factory=lambda: utcnow() + timedelta(hours=6))
    accounts: list[dict[str, Any]] = field(default_factory=list)
    account_info: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return utcnow() < self.expires_at

    def auth_headers(self) -> dict[str, str]:
        return {"CST": self.cst, "X-SECURITY-TOKEN": self.security_token}

    def streaming_password(self) -> str:
        return f"CST-{self.cst}|XST-{self.security_token}"


class IGAuthenticator:
    """Gestisce login, refresh e switch account per UN ambiente."""

    def __init__(
        self,
        environment: IGEnvironment,
        config: IGConfig | None = None,
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self.environment = environment
        self.config = config or get_settings().ig
        self.credentials: IGCredentials = self.config.credentials(environment)
        self.base_url = self.config.base_url(environment)
        self._http = http or httpx.AsyncClient(base_url=self.base_url, timeout=self.config.timeout_s)
        self._own_http = http is None
        self.session: IGSession | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.credentials.configured

    def base_headers(self, version: int = 1) -> dict[str, str]:
        if not self.credentials.api_key:
            raise ConfigError(f"IG {self.environment.value}: API key mancante")
        return {
            "X-IG-API-KEY": self.credentials.api_key.get_secret_value(),
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "Version": str(version),
        }

    async def authenticate(self, *, force: bool = False) -> IGSession:
        """POST /session v2 -> CST + X-SECURITY-TOKEN negli header di risposta."""
        if self.session and self.session.valid and not force:
            return self.session
        async with self._lock:
            if self.session and self.session.valid and not force:
                return self.session
            if not self.configured:
                raise ConfigError(
                    f"IG {self.environment.value} non configurato: servono API key, username, password"
                )
            assert self.credentials.password and self.credentials.username
            payload = {
                "identifier": self.credentials.username,
                "password": self.credentials.password.get_secret_value(),
                "encryptedPassword": False,
            }
            # backoff sull'allowance IG (403 exceeded-*): la chiave demo e' throttlata a burst,
            # si ricarica da sola. Meglio attendere che fallire subito.
            delay = 20.0
            response = await self._http.post("/session", headers=self.base_headers(2), json=payload)
            for _ in range(3):
                if response.status_code != 403 or "allowance" not in _error_code(response):
                    break
                log.warning("ig.login.allowance_backoff", environment=self.environment.value, delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 90.0)
                response = await self._http.post("/session", headers=self.base_headers(2), json=payload)
            if response.status_code >= 400:
                raise UpstreamError(
                    f"IG login {self.environment.value} fallito: HTTP {response.status_code} "
                    f"{_error_code(response)}",
                    status_code=response.status_code,
                    provider="ig",
                )
            body = response.json()
            cst = response.headers.get("CST")
            xst = response.headers.get("X-SECURITY-TOKEN")
            if not cst or not xst:
                raise UpstreamError("IG login: token di sessione mancanti", provider="ig")
            account_id = self.credentials.account_id or body.get("currentAccountId")
            session = IGSession(
                environment=self.environment,
                cst=cst,
                security_token=xst,
                account_id=str(account_id),
                lightstreamer_endpoint=str(body.get("lightstreamerEndpoint", "")),
                currency=str(body.get("currencyIsoCode") or self.config.default_currency),
                account_type=str(body.get("accountType") or ""),
                expires_at=utcnow() + timedelta(seconds=self.config.session_ttl_s),
                accounts=list(body.get("accounts") or []),
                account_info=dict(body.get("accountInfo") or {}),
            )
            # Patch sez. 23/41: mai mescolare ambienti. Il tipo conto deve combaciare.
            demo_accounts = {a.get("accountId") for a in session.accounts if str(a.get("accountType", "")).upper() != "LIVE"}
            if body.get("accounts"):
                is_demo_account = account_id in demo_accounts or "demo" in self.base_url
                if self.environment is IGEnvironment.LIVE and "demo" in self.base_url:
                    raise ConfigError("ambiente LIVE con base URL demo: configurazione incoerente")
                if self.environment is IGEnvironment.DEMO and not is_demo_account:
                    raise ConfigError("ambiente DEMO ma il conto risulta LIVE: rifiuto la sessione")
            if body.get("currentAccountId") and account_id != body.get("currentAccountId"):
                await self._switch_account(session, str(account_id))
            self.session = session
            log.info(
                "ig.login.ok",
                environment=self.environment.value,
                account_id=session.account_id,
                currency=session.currency,
            )
            return session

    async def _switch_account(self, session: IGSession, account_id: str) -> None:
        response = await self._http.put(
            "/session",
            headers={**self.base_headers(1), **session.auth_headers()},
            json={"accountId": account_id, "defaultAccount": False},
        )
        if response.status_code >= 400 and response.status_code != 412:
            raise UpstreamError(
                f"IG switch account fallito: HTTP {response.status_code} {_error_code(response)}",
                provider="ig",
            )
        if response.headers.get("CST"):
            session.cst = response.headers["CST"]
        if response.headers.get("X-SECURITY-TOKEN"):
            session.security_token = response.headers["X-SECURITY-TOKEN"]
        session.account_id = account_id

    async def refresh_session(self) -> IGSession:
        """IG non ha refresh per la v2: si rifa il login (patch sez. 3 refresh_session)."""
        return await self.authenticate(force=True)

    async def session_details(self) -> dict[str, Any]:
        session = await self.authenticate()
        response = await self._http.get(
            "/session", headers={**self.base_headers(1), **session.auth_headers()}
        )
        if response.status_code >= 400:
            raise UpstreamError(f"IG GET /session: HTTP {response.status_code}", provider="ig")
        return response.json()

    async def logout(self) -> None:
        if not self.session:
            return
        try:
            await self._http.delete(
                "/session", headers={**self.base_headers(1), **self.session.auth_headers()}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ig.logout.failed", error=str(exc)[:120])
        finally:
            self.session = None

    def headers(self, version: int = 1) -> dict[str, str]:
        if not self.session:
            raise UpstreamError("IG: sessione assente, chiamare authenticate()", provider="ig")
        return {**self.base_headers(version), **self.session.auth_headers()}

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("errorCode", ""))
    except Exception:  # noqa: BLE001
        return response.text[:120]
