"""Cache obbligatoria per news, wallet profile, embeddings, mapping (sez. 42/44).

Backend Redis quando disponibile, fallback in-memory con TTL. Interfaccia unica
per non accoppiare i collector a Redis.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Protocol

import orjson

from core.clock import utcnow
from core.logging import get_logger

log = get_logger("core.cache")


class CacheBackend(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl_s: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, amount: float = 1.0, ttl_s: float | None = None) -> float: ...
    async def close(self) -> None: ...


class MemoryCache:
    """Cache in-memory con TTL (default quando Redis non c'e)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires = item
            if expires is not None and utcnow().timestamp() > expires:
                self._data.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: bytes, ttl_s: float | None = None) -> None:
        async with self._lock:
            expires = utcnow().timestamp() + ttl_s if ttl_s else None
            self._data[key] = (value, expires)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def incr(self, key: str, amount: float = 1.0, ttl_s: float | None = None) -> float:
        async with self._lock:
            current = 0.0
            item = self._data.get(key)
            if item is not None:
                value, expires = item
                if expires is None or utcnow().timestamp() <= expires:
                    current = float(orjson.loads(value))
            new = current + amount
            expires_at = utcnow().timestamp() + ttl_s if ttl_s else (
                item[1] if item else None
            )
            self._data[key] = (orjson.dumps(new), expires_at)
            return new

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    """Backend Redis (sez. 44: latest prices, event state, cache, lock)."""

    def __init__(self, url: str):
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=False)

    async def get(self, key: str) -> bytes | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: bytes, ttl_s: float | None = None) -> None:
        if ttl_s:
            await self._redis.set(key, value, ex=int(max(1, ttl_s)))
        else:
            await self._redis.set(key, value)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def incr(self, key: str, amount: float = 1.0, ttl_s: float | None = None) -> float:
        value = await self._redis.incrbyfloat(key, amount)
        if ttl_s:
            await self._redis.expire(key, int(max(1, ttl_s)))
        return float(value)

    async def lock(self, name: str, timeout_s: float = 30.0) -> Any:
        """Distributed lock (sez. 44)."""
        return self._redis.lock(f"lock:{name}", timeout=timeout_s)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        await self._redis.aclose()


class Cache:
    """Facade tipizzata sopra il backend."""

    def __init__(self, backend: CacheBackend, namespace: str = "ats"):
        self.backend = backend
        self.namespace = namespace

    def _k(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    async def get_json(self, key: str) -> Any | None:
        raw = await self.backend.get(self._k(key))
        return orjson.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        await self.backend.set(self._k(key), orjson.dumps(value, default=str), ttl_s)

    async def delete(self, key: str) -> None:
        await self.backend.delete(self._k(key))

    async def incr(self, key: str, amount: float = 1.0, ttl_s: float | None = None) -> float:
        return await self.backend.incr(self._k(key), amount, ttl_s)

    async def get_or_set(
        self, key: str, factory: Any, ttl_s: float | None = None
    ) -> Any:
        cached = await self.get_json(key)
        if cached is not None:
            return cached
        value = factory() if not asyncio.iscoroutinefunction(factory) else await factory()
        if value is not None:
            await self.set_json(key, value, ttl_s)
        return value

    async def close(self) -> None:
        await self.backend.close()


def hash_key(*parts: Any) -> str:
    payload = orjson.dumps(parts, default=str)
    return hashlib.sha256(payload).hexdigest()[:32]


_cache: Cache | None = None


async def get_cache(redis_url: str | None = None) -> Cache:
    """Restituisce la cache di processo, con fallback automatico su memoria."""
    global _cache
    if _cache is not None:
        return _cache
    if redis_url:
        try:
            backend: CacheBackend = RedisCache(redis_url)
            if isinstance(backend, RedisCache) and await backend.ping():
                _cache = Cache(backend)
                log.info("cache.redis.connected")
                return _cache
            await backend.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("cache.redis.unavailable", error=str(exc))
    _cache = Cache(MemoryCache())
    log.info("cache.memory.enabled")
    return _cache


def set_cache(cache: Cache) -> None:
    global _cache
    _cache = cache


def reset_cache() -> None:
    global _cache
    _cache = None
