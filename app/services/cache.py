"""Simple in-memory TTL cache for tool results.

Why in-memory and not Redis: keeps the dependency tree small. For single-server
deployments serving up to a few hundred users this is more than enough.
For multi-server setups, swap the dict for a Redis client behind the same API.
"""
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class TTLCache:
    """Async-safe TTL cache. Per-user namespacing prevents data leakage."""

    def __init__(self, default_ttl: float = 300.0, max_entries: int = 10_000):
        self._store: dict[str, _CacheEntry] = {}
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(*parts: Any) -> str:
        """Hash of stringified args. Stable across runs because we sort dicts."""
        normalized = json.dumps(parts, default=str, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.is_expired:
                self._store.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        async with self._lock:
            if len(self._store) >= self._max_entries:
                # Evict expired or oldest
                self._evict_one()
            self._store[key] = _CacheEntry(
                value=value,
                expires_at=time.time() + (ttl or self._default_ttl),
            )

    async def invalidate_user(self, user_key: str) -> int:
        """Drop all entries belonging to a user (e.g. on logout)."""
        async with self._lock:
            keys = [k for k in self._store if k.startswith(user_key)]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    def _evict_one(self) -> None:
        """Drop the oldest expired entry, or oldest entry if none expired."""
        now = time.time()
        for k, e in list(self._store.items()):
            if e.expires_at <= now:
                self._store.pop(k, None)
                return
        # Nothing expired — drop oldest
        oldest = min(self._store.items(), key=lambda kv: kv[1].expires_at)
        self._store.pop(oldest[0], None)

    def stats(self) -> dict[str, Any]:
        now = time.time()
        active = sum(1 for e in self._store.values() if e.expires_at > now)
        return {"total": len(self._store), "active": active}


# Module-level shared cache. Default 5 minutes — most ERP queries are stable
# at that timescale (sales summaries, customer lists, etc.).
tool_cache = TTLCache(default_ttl=300.0)
