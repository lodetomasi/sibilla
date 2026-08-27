"""Client Limitless Exchange (prediction market on-chain su Base, YES/NO).

Fase 1: SOLO lettura (mercati, book, prezzi) — nessun wallet, nessuna chiave.
L'esecuzione on-chain (EIP-712 + USDC) e' un modulo separato, attivabile quando
l'utente fornisce wallet+chiave in modo sicuro. Riusa HttpClient (proxy/DoH, rate limit).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from core.http import HttpClient
from core.logging import get_logger

log = get_logger("execution.limitless.client")

API = "https://api.limitless.exchange"


def sign_request(token_id: str, secret_b64: str, method: str, path: str, body: str = "") -> dict[str, str]:
    """Firma HMAC-SHA256 richiesta dallo schema Limitless (header lmts-*).

    message = "{ISO8601}\\n{METHOD}\\n{path+query}\\n{body}"; chiave = base64decode(secret).
    """
    timestamp = datetime.now(UTC).isoformat()
    message = f"{timestamp}\n{method.upper()}\n{path}\n{body}"
    signature = base64.b64encode(
        hmac.new(base64.b64decode(secret_b64), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {"lmts-api-key": token_id, "lmts-timestamp": timestamp, "lmts-signature": signature}
# categorie/tag "generalisti" utili al nostro edge model-vs-market (no scalping 5-min)
GENERALIST = {"crypto", "bitcoin", "ethereum", "macro", "finance", "politics", "business", "tech", "economy", "hourly", "daily", "geopolitics"}
NOISE_TAGS = {"minutely", "1 min", "5 min", "10 min", "15 min", "30 min", "hourly"}


class LimitlessClient:
    def __init__(self, *, host: str = API, proxy: str | None = None, http: HttpClient | None = None, rps: float = 5.0, api_key: str | None = None, api_secret: str | None = None):
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.http = http or HttpClient(host, provider="limitless", rps=rps, timeout_s=20.0, proxy=proxy)

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def authenticated_get(self, path: str) -> Any:
        """GET firmato HMAC (profilo, saldi, posizioni). `path` include l'eventuale query."""
        if not self.authenticated:
            raise RuntimeError("credenziali HMAC Limitless assenti")
        headers = sign_request(self.api_key, self.api_secret, "GET", path, "")
        return await self.http.get_json(path, headers=headers)

    async def profile(self) -> dict[str, Any]:
        """GET /profiles/me: conferma le credenziali e restituisce il profilo autenticato."""
        return await self.authenticated_get("/profiles/me")

    async def active_markets(self, *, limit: int = 100, page: int | None = None) -> list[dict[str, Any]]:
        url = f"/markets/active?limit={limit}" + (f"&page={page}" if page else "")
        payload = await self.http.get_json(url)
        if isinstance(payload, dict):
            return payload.get("data") or next((v for v in payload.values() if isinstance(v, list)), [])
        return payload or []

    async def market(self, market_id: str) -> dict[str, Any] | None:
        try:
            return await self.http.get_json(f"/markets/{market_id}")
        except Exception as exc:  # noqa: BLE001
            log.info("limitless.market.not_found", market=market_id, error=str(exc)[:120])
            return None

    async def orderbook(self, market_id: str) -> dict[str, Any] | None:
        for path in (f"/markets/{market_id}/orderbook", f"/orderbook/{market_id}"):
            try:
                return await self.http.get_json(path)
            except Exception:  # noqa: BLE001
                continue
        return None

    async def generalist_markets(self, *, pages: int = 8, page_size: int = 25) -> list[dict[str, Any]]:
        """Mercati non-scalping in categorie con canale informativo (edge model-vs-market)."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            batch = await self.active_markets(limit=page_size, page=page)
            batch = [m for m in batch if str(m.get("id")) not in seen]
            for m in batch:
                seen.add(str(m.get("id")))
            if not batch:
                break
            for m in batch:
                cats = {str(c).lower() for c in (m.get("categories") or [])}
                tags = {str(t).lower() for t in (m.get("tags") or [])}
                if (cats | tags) & NOISE_TAGS:
                    continue
                if (cats | tags) & GENERALIST or _far_expiry(m):
                    out.append(m)
            if len(batch) < page_size:
                break
        return out

    async def aclose(self) -> None:
        await self.http.aclose()


def parse_market(m: dict[str, Any]) -> dict[str, Any]:
    """Normalizza un mercato Limitless: YES/NO, prezzi, condition id, scadenza."""
    prices = m.get("prices") or []
    tokens = m.get("tokens") or []
    yes = float(prices[0]) / (100 if prices and prices[0] > 1 else 1) if prices else None
    no = float(prices[1]) / (100 if len(prices) > 1 and prices[1] > 1 else 1) if len(prices) > 1 else (1 - yes if yes is not None else None)
    return {
        "venue": "limitless",
        "id": str(m.get("id")),
        "condition_id": m.get("conditionId"),
        "title": m.get("title") or m.get("proxyTitle"),
        "categories": m.get("categories") or [],
        "tags": m.get("tags") or [],
        "collateral": (m.get("collateralToken") or {}).get("symbol") if isinstance(m.get("collateralToken"), dict) else m.get("collateralToken"),
        "yes_price": yes,
        "no_price": no,
        "volume": _f(m.get("volume")),
        "expiration": m.get("expirationDate"),
        "tokens": tokens,
        "status": m.get("status"),
    }


def implied_probability(market: dict[str, Any]) -> float | None:
    """YES price = probabilita implicita di mercato (0-1)."""
    return market.get("yes_price")


def _far_expiry(m: dict[str, Any]) -> bool:
    ts = m.get("expirationTimestamp")
    if not ts:
        return False
    try:
        exp = datetime.fromtimestamp(float(ts) / (1000 if float(ts) > 1e11 else 1), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return False
    return (exp - datetime.now(UTC)).total_seconds() > 6 * 3600  # oltre l'intraday-scalping


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
