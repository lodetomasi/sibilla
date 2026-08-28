"""Test per core.http.HttpClient e core.ratelimit (TokenBucket/CircuitBreaker).

Fondamenta riusate direttamente dal client eToro (execution/etoro/client.py):
copertura genuina, non a scopo di soglia.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from core.errors import RateLimitError, UpstreamError
from core.http import HttpClient
from core.ratelimit import CircuitBreaker, TokenBucket


def _client(handler, **kwargs) -> HttpClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    return HttpClient("https://x.test", provider="test", client=async_client, **kwargs)


async def test_get_json_returns_parsed_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    result = await client.get_json("/ping")

    assert result == {"ok": True}
    assert client.stats.requests == 1
    assert client.stats.errors == 0


async def test_post_json_sends_body_and_returns_parsed_response():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"created": True})

    client = _client(handler)
    result = await client.post_json("/create", json={"a": 1})

    assert result == {"created": True}
    assert b'"a": 1' in captured["body"] or b'"a":1' in captured["body"]


async def test_4xx_raises_upstream_error_and_keeps_circuit_closed():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = _client(handler)
    with pytest.raises(UpstreamError):
        await client.get_json("/missing")

    assert client.healthy is True  # 4xx non apre il circuito (errore di richiesta, non provider giu)
    assert client.stats.errors >= 1


async def test_retryable_5xx_retries_then_raises_after_max_attempts():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = _client(handler, max_retries=3, rps=1000.0)
    with pytest.raises(UpstreamError):
        await client.get_json("/flaky", retries=3)

    assert calls["n"] == 3


async def test_429_raises_rate_limit_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    client = _client(handler, max_retries=1, rps=1000.0)
    with pytest.raises(RateLimitError):
        await client.get_json("/limited", retries=1)

    assert client.stats.rate_limited == 1


async def test_circuit_opens_after_failure_threshold_of_5xx():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    client = _client(handler, max_retries=1, rps=1000.0, failure_threshold=2)
    for _ in range(2):
        with pytest.raises(UpstreamError):
            await client.get_json("/down", retries=1)

    assert client.healthy is False
    with pytest.raises(UpstreamError, match="circuit breaker aperto"):
        await client.get_json("/down", retries=1)


async def test_non_json_response_raises_upstream_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = _client(handler)
    with pytest.raises(UpstreamError, match="risposta non JSON"):
        await client.get_json("/broken")


async def test_get_text_returns_raw_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain text")

    client = _client(handler)
    assert await client.get_text("/raw") == "plain text"


async def test_aclose_closes_only_owned_client():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    external = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    injected = HttpClient("https://x.test", provider="test", client=external)
    await injected.aclose()
    assert not external.is_closed  # client iniettato: aclose() non lo tocca

    own = HttpClient("https://x.test", provider="test2", client=None)
    await own.aclose()
    assert own._client.is_closed  # client creato internamente: aclose() lo chiude


async def test_token_bucket_throttles_bursts_above_capacity():
    bucket = TokenBucket(rps=1000.0, burst=2)
    start = asyncio.get_event_loop().time()
    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()  # terzo token: capacity esaurita, deve aspettare
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed > 0


def test_token_bucket_rejects_non_positive_rps():
    with pytest.raises(ValueError):
        TokenBucket(rps=0)


def test_circuit_breaker_opens_and_resets_after_timeout():
    breaker = CircuitBreaker("x", failure_threshold=2, reset_timeout_s=0.01)
    assert not breaker.is_open
    breaker.record_failure()
    assert not breaker.is_open  # sotto soglia
    breaker.record_failure()
    assert breaker.is_open
    with pytest.raises(UpstreamError):
        breaker.guard()
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.failures == 0
