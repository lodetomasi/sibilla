"""Client eToro: wrapper sottile su core.http.HttpClient.

Iron rule: nessuna reimplementazione di rate limit/retry/circuit breaker qui,
si riusa HttpClient (gia' testato, gia' usato dagli altri collector). Due
istanze separate perche' eToro ha rate limit diversi per GET (60/min) e
scrittura (20/min): qui si resta sotto soglia (55/18).
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from core.config import Settings, get_settings
from core.http import HttpClient
from core.logging import get_logger

log = get_logger("execution.etoro.client")


class EtoroClient:
    def __init__(self, *, settings: Settings | None = None, transport: httpx.MockTransport | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings.etoro
        base_url = cfg.base_url(self.settings.execution_mode)
        headers = {
            "x-api-key": cfg.api_key.get_secret_value() if cfg.api_key else "",
            "x-user-key": cfg.user_key.get_secret_value() if cfg.user_key else "",
            "Content-Type": "application/json",
        }
        http_client = None
        if transport is not None:
            http_client = httpx.AsyncClient(base_url=base_url, transport=transport, headers=headers)
        self._read = HttpClient(
            base_url,
            provider="etoro-read",
            rps=cfg.read_rate_limit_per_min / 60,
            timeout_s=cfg.request_timeout_s,
            headers=headers,
            client=http_client,
        )
        self._write = HttpClient(
            base_url,
            provider="etoro-write",
            rps=cfg.write_rate_limit_per_min / 60,
            timeout_s=cfg.request_timeout_s,
            headers=headers,
            client=http_client,
        )

    def _request_id(self) -> dict[str, str]:
        return {"x-request-id": str(uuid.uuid4())}

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._read.get_json(path, params=params, headers=self._request_id())

    async def post(self, path: str, *, json: dict[str, Any]) -> Any:
        return await self._write.post_json(path, json=json, headers=self._request_id())

    async def post_order(self, payload: dict[str, Any]) -> Any:
        path = self.settings.etoro.orders_path(self.settings.execution_mode)
        return await self.post(path, json=payload)

    async def aclose(self) -> None:
        await self._read.aclose()
        if self._write is not self._read:
            await self._write.aclose()
