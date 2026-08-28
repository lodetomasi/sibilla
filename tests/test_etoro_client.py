from __future__ import annotations

import httpx
import pytest

from core.config import EtoroConfig, Settings
from core.enums import ExecutionMode
from core.errors import UpstreamError
from execution.etoro.client import EtoroClient


def _settings(mode: ExecutionMode) -> Settings:
    s = Settings()
    s.execution_mode = mode
    s.etoro = EtoroConfig(api_key="pub-key", user_key="user-key")
    return s


@pytest.mark.asyncio
async def test_get_sends_auth_headers_and_request_id() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = EtoroClient(settings=_settings(ExecutionMode.DEMO), transport=transport)
    result = await client.get("/api/v1/watchlists")

    assert result == {"ok": True}
    assert captured["headers"]["x-api-key"] == "pub-key"
    assert captured["headers"]["x-user-key"] == "user-key"
    assert len(captured["headers"]["x-request-id"]) == 36  # UUID4 con trattini
    await client.aclose()


@pytest.mark.asyncio
async def test_two_calls_use_different_request_ids() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-request-id"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    client = EtoroClient(settings=_settings(ExecutionMode.DEMO), transport=transport)
    await client.get("/api/v1/watchlists")
    await client.get("/api/v1/watchlists")

    assert seen[0] != seen[1]
    await client.aclose()


@pytest.mark.asyncio
async def test_demo_mode_uses_demo_orders_path() -> None:
    captured_url: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_url["path"] = request.url.path
        return httpx.Response(200, json={"orderId": "1"})

    transport = httpx.MockTransport(handler)
    client = EtoroClient(settings=_settings(ExecutionMode.DEMO), transport=transport)
    await client.post_order({"action": "open"})

    assert captured_url["path"] == "/api/v2/trading/execution/demo/orders"
    await client.aclose()


@pytest.mark.asyncio
async def test_live_mode_uses_real_orders_path() -> None:
    captured_url: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_url["path"] = request.url.path
        return httpx.Response(200, json={"orderId": "1"})

    transport = httpx.MockTransport(handler)
    client = EtoroClient(settings=_settings(ExecutionMode.LIVE), transport=transport)
    await client.post_order({"action": "open"})

    assert captured_url["path"] == "/api/v2/trading/execution/orders"
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_error_on_4xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    transport = httpx.MockTransport(handler)
    client = EtoroClient(settings=_settings(ExecutionMode.DEMO), transport=transport)
    with pytest.raises(UpstreamError):
        await client.get("/api/v1/watchlists")
    await client.aclose()


def test_read_and_write_buckets_configured_under_etoro_rate_limits() -> None:
    # Verifica diretta della configurazione del TokenBucket (rps), non un test a
    # tempo reale con N richieste: TokenBucket.acquire() throttla per costruzione,
    # quindi il rate non puo' MAI superare rps una volta che rps e' corretto — un
    # test a tempo reale con 100 richieste a 18/60 richiederebbe ~5.5s ed e'
    # soggetto a jitter di scheduling (flaky in CI). Questo test e' equivalente
    # nell'intento (criterio #4 del design doc) ma deterministico e istantaneo.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = EtoroClient(settings=_settings(ExecutionMode.DEMO), transport=httpx.MockTransport(handler))
    assert client._read.bucket.rps == pytest.approx(55 / 60)
    assert client._write.bucket.rps == pytest.approx(18 / 60)
    assert client._read.bucket.rps < 60 / 60  # sempre sotto il limite reale eToro (60/min)
    assert client._write.bucket.rps < 20 / 60  # sempre sotto il limite reale eToro (20/min)
