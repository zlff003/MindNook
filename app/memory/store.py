from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

import redis
from redis.exceptions import RedisError

from app.core.config import Settings


class InMemoryRedisStore:
    """Small redis-py compatible list store used by deterministic tests."""

    def __init__(self):
        self._lists: dict[str, list[str]] = defaultdict(list)
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def _purge(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._lists.pop(key, None)
            self._expires_at.pop(key, None)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        with self._lock:
            self._purge(key)
            values = self._lists.get(key, [])
            actual_end = len(values) if end == -1 else end + 1
            return list(values[start:actual_end])

    def rpush(self, key: str, *values: str) -> int:
        with self._lock:
            self._purge(key)
            self._lists[key].extend(values)
            return len(self._lists[key])

    def ltrim(self, key: str, start: int, end: int) -> bool:
        with self._lock:
            self._purge(key)
            values = self._lists.get(key, [])
            normalized_start = max(0, len(values) + start) if start < 0 else start
            actual_end = len(values) if end == -1 else end + 1
            if end < -1:
                actual_end = max(0, len(values) + end + 1)
            self._lists[key] = list(values[normalized_start:actual_end])
            return True

    def expire(self, key: str, seconds: int) -> bool:
        with self._lock:
            if key not in self._lists:
                return False
            self._expires_at[key] = time.monotonic() + seconds
            return True

    def ttl(self, key: str) -> int:
        with self._lock:
            self._purge(key)
            if key not in self._lists:
                return -2
            if key not in self._expires_at:
                return -1
            return max(0, int(self._expires_at[key] - time.monotonic()))

    def delete(self, key: str) -> int:
        with self._lock:
            existed = key in self._lists
            self._lists.pop(key, None)
            self._expires_at.pop(key, None)
            return int(existed)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class CircuitBreakingRedisStore:
    """Avoid repeated socket timeouts when Redis is temporarily unavailable."""

    def __init__(self, client, cooldown_seconds: float = 5.0):
        self.client = client
        self.cooldown_seconds = cooldown_seconds
        self._unavailable_until = 0.0
        self._lock = threading.Lock()

    def _call(self, method: str, *args):
        with self._lock:
            if time.monotonic() < self._unavailable_until:
                raise OSError("redis circuit is open")
        try:
            return getattr(self.client, method)(*args)
        except (RedisError, OSError):
            with self._lock:
                self._unavailable_until = time.monotonic() + self.cooldown_seconds
            raise

    def lrange(self, key: str, start: int, end: int):
        return self._call("lrange", key, start, end)

    def rpush(self, key: str, *values: str):
        return self._call("rpush", key, *values)

    def ltrim(self, key: str, start: int, end: int):
        return self._call("ltrim", key, start, end)

    def expire(self, key: str, seconds: int):
        return self._call("expire", key, seconds)

    def delete(self, key: str):
        return self._call("delete", key)

    def ping(self):
        return self._call("ping")

    def close(self) -> None:
        self.client.close()


def build_redis_store(settings: Settings) -> Any:
    if settings.app_env == "test" or settings.redis_url.startswith("memory://"):
        return InMemoryRedisStore()
    return CircuitBreakingRedisStore(
        redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_connect_timeout_seconds,
            retry_on_timeout=False,
        )
    )
