from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, max_size: int = 2000) -> None:
        self.max_size = max_size
        self._data: dict[str, _Entry[T]] = {}
        self._lock = Lock()

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if entry.expires_at < monotonic():
                self._data.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: T, ttl_seconds: int) -> None:
        expires_at = monotonic() + max(ttl_seconds, 1)
        with self._lock:
            if len(self._data) >= self.max_size:
                # Remove one arbitrary key to keep memory bounded.
                self._data.pop(next(iter(self._data)))
            self._data[key] = _Entry(value=value, expires_at=expires_at)
