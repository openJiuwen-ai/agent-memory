"""Process-local ownership for SDK continuity runtimes.

Requests carry their own trusted context. Leases share resources, never request state.
Only this pool closes a runtime; cancellation of an async waiter never cancels a write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict, dataclass
from typing import Any

from jiuwen_memory.api import (
    ConflictError,
    RateLimitedError,
    ValidationError,
)
from jiuwen_memory.api import (
    sdk_continuity_runtime_identity as runtime_identity,
)

logger = logging.getLogger(__name__)


class OperationWaitTimeout(TimeoutError):
    """Waiting ended; the tracked SDK operation may still commit."""


def principal_key(security) -> str:
    return json.dumps(asdict(security.auth.actor), sort_keys=True)


@dataclass
class _Entry:
    signature: str
    principal: str
    executor: ThreadPoolExecutor
    max_workers: int
    max_queue_size: int
    runtime: Any = None
    portal: Any = None
    state: str = "starting"
    references: int = 0
    pending: int = 0
    error: BaseException | None = None


@dataclass(eq=False)
class _Lease:
    key: str
    entry: _Entry
    active: bool = True


class _SharedSDKRuntimePool:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _Entry] = {}

    def acquire(self, config: dict, principal: str, factory: Callable[[], Any]) -> _Lease:
        globals_ = config.get("globals", {})
        max_workers = globals_.get("sdk_max_workers", 16)
        max_queue_size = globals_.get("sdk_max_queue_size", 256)
        if type(max_workers) is not int or max_workers < 1:
            raise ValidationError("sdk_max_workers must be a positive integer")
        if type(max_queue_size) is not int or max_queue_size < 0:
            raise ValidationError("sdk_max_queue_size must be a nonnegative integer")
        # Explicit defaults and omitted defaults must share the same runtime.
        normalized = {**config, "globals": {**globals_, "sdk_max_workers": max_workers,
                                          "sdk_max_queue_size": max_queue_size}}
        key, signature = runtime_identity(normalized)
        with self._condition:
            while True:
                entry = self._entries.get(key)
                if entry is None:
                    break
                if entry.signature != signature or entry.principal != principal:
                    raise ConflictError(
                        "runtime", message="incompatible SDK Registry configuration",
                    )
                if entry.state == "failed":
                    raise RuntimeError("previous SDK runtime cleanup failed") from entry.error
                if entry.state == "open":
                    entry.references += 1
                    logger.info("[continuity.runtime.reuse] runtime_id=%s references=%s pending=%s",
                                id(entry.runtime), entry.references, entry.pending)
                    self._condition.notify_all()
                    return _Lease(key, entry)
                self._condition.wait()
            entry = _Entry(signature, principal, ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="memory-sdk",
            ), max_workers, max_queue_size)
            self._entries[key] = entry
        try:
            def create():
                entry.runtime = factory()
                entry.runtime.start()

            entry.executor.submit(copy_context().run, create).result()
        except BaseException as exc:
            cleanup = None
            if entry.runtime is not None:
                try:
                    entry.runtime.close(wait=True)
                except BaseException as error:
                    cleanup = error
            entry.executor.shutdown(wait=True)
            with self._condition:
                if cleanup is None:
                    self._entries.pop(key, None)
                else:
                    entry.state, entry.error = "failed", cleanup
                self._condition.notify_all()
            if cleanup is not None:
                raise BaseExceptionGroup(
                    "SDK runtime startup and cleanup failed", [exc, cleanup],
                ) from exc
            raise
        with self._condition:
            entry.state, entry.references = "open", 1
            logger.info("[continuity.runtime.created] runtime_id=%s references=1 pending=0 "
                        "max_workers=%s max_queue_size=%s", id(entry.runtime),
                        max_workers, max_queue_size)
            self._condition.notify_all()
            return _Lease(key, entry)

    def attach_portal(self, lease: _Lease, factory: Callable[[Any], Any]) -> Any:
        with self._condition:
            entry = lease.entry
            if not lease.active or entry.state != "open":
                raise RuntimeError("SDK runtime lease is closed")
            # The Portal factory also checks identity, port and framing compatibility.
            entry.portal = factory(entry.runtime)
            return entry.portal

    async def submit(self, lease: _Lease, operation: Callable[[], Any], *, timeout=120.0):
        """Admit once; release capacity only when the underlying call returns or raises.

        Timeout/cancellation ends only this waiter. A queued call remains queued and
        an executing call keeps running; neither is automatically retried.
        """
        with self._condition:
            entry = lease.entry
            if not lease.active or entry.state != "open":
                raise RuntimeError("SDK runtime lease is closed")
            # Reserve worker and waiting capacity atomically across all Providers.
            # A cancelled/timed-out waiter retains its slot until the operation ends.
            if entry.pending >= entry.max_workers + entry.max_queue_size:
                raise RateLimitedError("SDK runtime queue is full; retry later")
            entry.pending += 1
            try:
                future = entry.executor.submit(copy_context().run, operation)
            except BaseException:
                entry.pending -= 1
                self._condition.notify_all()
                raise

            def done(_):
                with self._condition:
                    entry.pending -= 1
                    self._condition.notify_all()

            future.add_done_callback(done)
        wrapped = asyncio.wrap_future(future)
        # Consume late exceptions after a timeout/cancel without losing ownership tracking.
        wrapped.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None
        )
        waiter = asyncio.ensure_future(asyncio.shield(wrapped))
        try:
            completed, _ = await asyncio.wait({waiter}, timeout=timeout)
        except asyncio.CancelledError:
            waiter.cancel()
            logger.warning("[continuity.runtime.wait_cancelled] runtime_id=%s "
                           "operation_still_tracked=true",
                           id(entry.runtime))
            raise
        if not completed:
            waiter.cancel()  # Only this shield; the concurrent future remains tracked.
            logger.warning("[continuity.runtime.wait_timeout] runtime_id=%s "
                           "operation_still_tracked=true",
                           id(entry.runtime))
            raise OperationWaitTimeout("SDK operation is still pending; result is unknown")
        return waiter.result()

    def release(self, lease: _Lease) -> None:
        with self._condition:
            if not lease.active:
                return
            lease.active = False
            entry = lease.entry
            entry.references -= 1
            logger.info("[continuity.runtime.release] runtime_id=%s references=%s pending=%s",
                        id(entry.runtime), entry.references, entry.pending)
            while entry.references == 0 and entry.pending:
                self._condition.wait()
            if entry.references:
                return
            if entry.state != "open":
                return
            entry.state = "closing"
        try:
            try:
                if entry.portal is not None:
                    entry.portal.close()
                entry.runtime.close(wait=True)
            finally:
                entry.executor.shutdown(wait=True)
        except BaseException as exc:
            with self._condition:
                entry.state, entry.error = "failed", exc
                self._condition.notify_all()
            raise
        with self._condition:
            self._entries.pop(lease.key, None)
            logger.info("[continuity.runtime.closed] runtime_id=%s", id(entry.runtime))
            self._condition.notify_all()

    def release_in_background(self, lease: _Lease) -> Future:
        """Start cleanup before returning, independently of the caller's event loop."""
        result = Future()
        result.set_running_or_notify_cancel()

        def cleanup():
            try:
                self.release(lease)
            except BaseException as exc:
                result.set_exception(exc)
            else:
                result.set_result(None)

        threading.Thread(target=cleanup, name="memory-sdk-release", daemon=True).start()
        return result


_SDK_RUNTIME_POOL = _SharedSDKRuntimePool()
