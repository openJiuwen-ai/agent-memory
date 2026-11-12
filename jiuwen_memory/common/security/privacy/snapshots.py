"""Bounded, process-local protected snapshots for one-use human disclosure."""

from __future__ import annotations

import secrets
import threading
import time

from jiuwen_memory.common.errors import RateLimitedError, ValidationError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps, loads


class DisclosureSnapshots:
    def __init__(self, *, limit: int = 256, max_bytes: int = 8 * 1024 * 1024):
        self._limit, self._max_bytes = limit, max_bytes
        self._items: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.RLock()

    def stage(self, unit: MemoryUnit) -> str:
        data = dumps(unit)
        with self._lock:
            now = time.monotonic()
            self._items = {key: row for key, row in self._items.items() if row[0] > now}
            if len(self._items) >= self._limit:
                raise RateLimitedError("human snapshot capacity exhausted")
            if len(data) + sum(len(row[1]) for row in self._items.values()) > self._max_bytes:
                raise RateLimitedError("human snapshot capacity exhausted")
            operation = secrets.token_urlsafe(32)
            self._items[operation] = (now + 300, data)
            return operation

    def consume(self, operation: str) -> MemoryUnit:
        with self._lock:
            row = self._items.pop(operation, None)
        if row is None or row[0] <= time.monotonic():
            raise ValidationError("human snapshot expired or already consumed")
        unit = loads(row[1])
        if unit is None:
            raise ValidationError("invalid human snapshot")
        return unit

    def discard(self, operation: str) -> None:
        with self._lock:
            self._items.pop(operation, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
