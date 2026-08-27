"""Client HTTP condiviso: rate limit, retry, circuit breaker, latency tracking.

Sez. 30 (latency tracking), 27 (kill switch su API unavailable), 78 (performance).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.clock import utcnow
from core.errors import RateLimitError, UpstreamError
from core.logging import get_logger
from core.ratelimit import CircuitBreaker, TokenBucket

log = get_logger("core.http")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


@dataclass
class HttpStats:
    """Statistiche per provider, lette dal kill switch e dalla dashboard."""

    requests: int = 0
    errors: int = 0
    rate_limited: int = 0
    total_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    last_error: str | None = None
    last_success_at: Any = None
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.requests if self.requests else 0.0

    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        idx = min(len(ordered) - 1, int(0.95 * len(ordered)))
        return ordered[idx]

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "rate_limited": self.rate_limited,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms(), 2),
            "last_latency_ms": round(self.last_latency_ms, 2),
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
        }


class HttpClient:
    """Wrapper httpx con politiche uniformi.

    Non solleva mai eccezioni httpx grezze verso l'alto: converte in UpstreamError
    cosi i collector hanno un contratto unico.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        provider: str = "http",
        rps: float = 5.0,
        timeout_s: float = 20.0,
        max_retries: int = 4,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        failure_threshold: int = 5,
        dns_bypass: str = "auto",
        proxy: str | None = None,
    ):
        """dns_bypass: "auto" attiva la risoluzione DNS-over-HTTPS + SNI esplicito quando il
        DNS locale dirotta il dominio (certificato non valido per l'host: tipico dei blocchi
        ISP); "always" la usa subito; "never" la disabilita.

        proxy: URL di un proxy (es. 'socks5://127.0.0.1:9050' per Tor, o l'endpoint SOCKS/HTTP
        di una VPN) attraverso cui uscire per questo provider. Se il proxy fallisce, si ricade
        sul percorso diretto + bypass DoH.
        """
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.bucket = TokenBucket(rps)
        self.breaker = CircuitBreaker(provider, failure_threshold=failure_threshold)
        self.stats = HttpStats()
        self._timeout_s = timeout_s
        self._headers = dict(headers or {})
        self._dns_bypass = dns_bypass
        self._bypass_active = False
        self._sni_host: str | None = None
        self._proxy = proxy or None
        self._own_client = client is None
        self._client = client or self._build_client(proxy=self._proxy)

    def _build_client(self, *, proxy: str | None = None, base_url: str | None = None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": base_url if base_url is not None else self.base_url,
            "timeout": httpx.Timeout(self._timeout_s),
            "headers": dict(self._headers),
            "follow_redirects": True,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        self.breaker.guard()
        attempts = self.max_retries if retries is None else retries
        delay = 0.5
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            await self.bucket.acquire()
            started = asyncio.get_event_loop().time()
            try:
                if self._dns_bypass == "always" and not self._bypass_active and self._own_client:
                    await self._enable_dns_bypass()
                extensions = {"sni_hostname": self._sni_host} if self._bypass_active and self._sni_host else None
                response = await self._client.request(
                    method, url, params=params, json=json, headers=headers, extensions=extensions
                )
                latency_ms = (asyncio.get_event_loop().time() - started) * 1000
                self.stats.requests += 1
                self.stats.total_latency_ms += latency_ms
                self.stats.last_latency_ms = latency_ms
                self.stats.latencies_ms.append(latency_ms)
                if len(self.stats.latencies_ms) > 500:
                    del self.stats.latencies_ms[:-500]

                if response.status_code in RETRYABLE_STATUS:
                    if response.status_code == 429:
                        self.stats.rate_limited += 1
                    raise UpstreamError(
                        f"{self.provider} HTTP {response.status_code}",
                        status_code=response.status_code,
                        provider=self.provider,
                    )
                if response.status_code >= 400:
                    self.stats.errors += 1
                    self.stats.last_error = f"HTTP {response.status_code}"
                    # 4xx = richiesta/risorsa sbagliata, non provider giu:
                    # non deve aprire il circuito (sez. 27: il kill switch guarda
                    # l'indisponibilita reale del provider).
                    self.breaker.record_success()
                    raise UpstreamError(
                        f"{self.provider} HTTP {response.status_code}: {response.text[:300]}",
                        status_code=response.status_code,
                        provider=self.provider,
                    )
                self.breaker.record_success()
                self.stats.last_success_at = utcnow()
                return response
            except (httpx.TimeoutException, httpx.TransportError, UpstreamError) as exc:
                last_exc = exc
                # proxy (VPN/Tor) irraggiungibile o exit bloccato: ricadi sul percorso diretto
                if self._proxy and self._own_client and isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                    log.warning("http.proxy.fallback_direct", provider=self.provider, proxy=self._proxy, error=str(exc)[:120])
                    await self._client.aclose()
                    self._proxy = None
                    self._client = self._build_client(proxy=None)
                    continue
                if (
                    self._dns_bypass == "auto"
                    and not self._bypass_active
                    and self._own_client
                    and _looks_like_dns_hijack(exc)
                ):
                    if await self._enable_dns_bypass():
                        log.warning("http.dns_bypass.enabled", provider=self.provider, host=self._sni_host)
                        continue
                self.stats.errors += 1
                self.stats.last_error = str(exc)[:200]
                status = getattr(exc, "status_code", None)
                fatal = isinstance(exc, UpstreamError) and status not in RETRYABLE_STATUS
                if fatal:
                    # errore definitivo lato richiesta: propaga senza toccare il circuito
                    raise
                if attempt >= attempts:
                    self.breaker.record_failure()
                    break
                log.warning(
                    "http.retry",
                    provider=self.provider,
                    url=url,
                    attempt=attempt,
                    error=str(exc)[:160],
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)

        self.breaker.record_failure()
        if isinstance(last_exc, UpstreamError):
            if last_exc.status_code == 429:
                raise RateLimitError(str(last_exc), status_code=429, provider=self.provider)
            raise last_exc
        raise UpstreamError(f"{self.provider}: {last_exc}", provider=self.provider)

    async def _enable_dns_bypass(self) -> bool:
        """Risolve l'host via DoH (Cloudflare/Google) e ricrea il client puntando all'IP
        reale con Host header e SNI corretti. Il certificato resta verificato sull'hostname."""
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.base_url)
        host = parts.hostname
        if not host:
            return False
        ips = await resolve_doh(host)
        if not ips:
            return False
        ip = ips[0]
        netloc = f"{ip}:{parts.port}" if parts.port else ip
        new_base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=new_base,
            timeout=httpx.Timeout(self._timeout_s),
            headers={**self._headers, "Host": host},
            follow_redirects=False,
        )
        self._sni_host = host
        self._bypass_active = True
        return True

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return _parse_json(response, self.provider)

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("POST", url, **kwargs)
        return _parse_json(response, self.provider)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        return response.text

    @property
    def healthy(self) -> bool:
        return not self.breaker.is_open

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()


_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
_doh_cache: dict[str, list[str]] = {}


async def resolve_doh(host: str) -> list[str]:
    """Risoluzione A record via DNS-over-HTTPS (bypassa il DNS locale dirottato)."""
    if host in _doh_cache:
        return _doh_cache[host]
    async with httpx.AsyncClient(timeout=8.0) as client:
        for endpoint in _DOH_ENDPOINTS:
            try:
                response = await client.get(
                    endpoint, params={"name": host, "type": "A"}, headers={"accept": "application/dns-json"}
                )
                if response.status_code != 200:
                    continue
                answers = response.json().get("Answer") or []
                ips = [a["data"] for a in answers if a.get("type") == 1]
                if ips:
                    _doh_cache[host] = ips
                    return ips
            except Exception as exc:  # noqa: BLE001
                log.info("doh.failed", endpoint=endpoint, error=str(exc)[:100])
    return []


def _looks_like_dns_hijack(exc: Exception) -> bool:
    """Hijack DNS (cert sbagliato) O risoluzione fallita (resolver locale rotto/hotspot):
    in entrambi i casi la via d'uscita e' risolvere via DoH e connettersi per IP."""
    text = str(exc)
    return ("CERTIFICATE_VERIFY_FAILED" in text or "Hostname mismatch" in text
            or "certificate is not valid" in text
            or "nodename nor servname" in text          # macOS: EAI_NONAME
            or "Name or service not known" in text      # Linux: EAI_NONAME
            or "Temporary failure in name resolution" in text)


def _parse_json(response: httpx.Response, provider: str) -> Any:
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            f"{provider}: risposta non JSON ({response.text[:200]})", provider=provider
        ) from exc
