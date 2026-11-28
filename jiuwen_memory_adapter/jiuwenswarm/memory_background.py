# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Service-owned scheduling for SDK document memory."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from threading import Lock
from typing import Any

from jiuwen_memory.api import (
    RequestSecurityContext,
    Scope,
    ValidationError,
    consolidation_interval,
    document_root,
    with_consolidation_scheduler,
)

_RUNTIMES: dict[str, MemoryBackground | None] = {}
_RUNTIMES_LOCK = Lock()


def background_for(root: str) -> MemoryBackground | None:
    with _RUNTIMES_LOCK:
        return _RUNTIMES.get(root)


def _owner(scope: Scope) -> tuple[str, str, str]:
    return scope.org, scope.space, scope.user


def track_memory_operation(method):
    """Count SDK memory calls through completion, including executor queue time."""
    parameters = signature(method)

    @wraps(method)
    async def tracked(self, *args, **kwargs):
        background = background_for(getattr(self, "_document_root", ""))
        if background is None:
            background = getattr(self, "_memory_background", None)
        if background is None:
            return await method(self, *args, **kwargs)
        scope = parameters.bind(self, *args, **kwargs).arguments["scope"]

        async def run():
            with background.operation(scope):
                return await method(self, *args, **kwargs)

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise

    return tracked


class MemoryBackground:
    """One background runtime for an SDK document root, owned by the host service."""

    def __init__(self, root: str, client: Any, interval: int) -> None:
        self._root = root
        self._client = client
        self._interval = interval
        self._lock = Lock()
        self._busy: Counter[tuple[str, str, str]] = Counter()
        self._registered: set[tuple[str, str, str]] = set()
        self._closing = False

    async def start(self) -> None:
        self._client.bind_memory_consolidation_readiness(self.ready)
        await self._client.initialize()
        await self._client.restore_dreaming()

    async def register(self, scope: Scope, security: RequestSecurityContext) -> None:
        owner = Scope(org=scope.org, space=scope.space, user=scope.user)
        actor = security.auth.actor
        if not owner.org or not owner.user:
            return
        if (actor.org, actor.user) != (owner.org, owner.user):
            return
        if actor.space and actor.space != owner.space:
            return
        key = _owner(owner)
        with self._lock:
            if self._closing or key in self._registered:
                return
        await asyncio.to_thread(
            self._client.ensure_memory_consolidation, owner,
            security=security, interval=self._interval,
        )
        with self._lock:
            self._registered.add(key)

    @contextmanager
    def operation(self, scope: Scope):
        key = _owner(scope)
        with self._lock:
            self._busy[key] += 1
        try:
            yield
        finally:
            with self._lock:
                self._busy[key] -= 1
                if not self._busy[key]:
                    del self._busy[key]

    async def ready(self, scope: Scope) -> bool:
        with self._lock:
            return not self._closing and not self._busy[_owner(scope)]

    async def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        try:
            await self._client.close()
        finally:
            with _RUNTIMES_LOCK:
                if _RUNTIMES.get(self._root) is self:
                    del _RUNTIMES[self._root]


async def start_memory_background(
    config: dict, build_client: Callable[[dict], Any],
) -> MemoryBackground | None:
    interval = consolidation_interval(config)
    if interval is None:
        return None
    root = document_root(config)
    background_config = with_consolidation_scheduler(config)
    with _RUNTIMES_LOCK:
        if root in _RUNTIMES:
            raise ValidationError("Document memory background runtime is already running")
        _RUNTIMES[root] = None
    try:
        service = MemoryBackground(root, build_client(background_config), interval)
    except BaseException:
        with _RUNTIMES_LOCK:
            del _RUNTIMES[root]
        raise
    with _RUNTIMES_LOCK:
        _RUNTIMES[root] = service
    try:
        await service.start()
    except BaseException:
        await service.close()
        raise
    return service
