# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import asyncio
import uuid
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


class DistributedLock:
    """
    Async multiprocess safe distributed lock with auto-renewal (watchdog).

    The lock TTL is fixed at 10s. A background watchdog coroutine renews
    the lock every RENEW_INTERVAL_SEC seconds (via ``renew_exclusive``)
    so that long-running operations (e.g. LLM extraction ~60s) never find
    the lock expired. If the process crashes, the watchdog stops renewing
    and the lock auto-releases after 10s — preventing deadlocks.
    """
    RENEW_INTERVAL_SEC = 3     # Watchdog renewal interval (must be < ttl)

    def __init__(self, store, lock_name: str):
        self.store = store
        self.lock_key = "_lock/" + lock_name
        self.ttl = 10
        self.retry_delay = 0.01
        self.lock_value = None
        self._watchdog_task: asyncio.Task | None = None
        self._released = False

    async def _watchdog(self):
        """Background coroutine that renews the lock every RENEW_INTERVAL_SEC
        seconds until ``release()`` is called or the lock is lost."""
        try:
            while not self._released:
                await asyncio.sleep(self.RENEW_INTERVAL_SEC)
                if self._released:
                    break
                renewed = await self.store.renew_exclusive(
                    self.lock_key, self.lock_value, expiry=self.ttl
                )
                if not renewed:
                    memory_logger.warning(
                        "Lock renewal failed — lock may have been lost or expired",
                        event_type=LogEventType.MEMORY_STORE,
                        lock_key=self.lock_key,
                    )
                    # Lock is lost; stop watchdog. The safety-net TTL (10s)
                    # ensures other waiters can acquire after expiry.
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            memory_logger.error(
                "Watchdog error",
                exception=str(e),
                event_type=LogEventType.MEMORY_STORE,
                lock_key=self.lock_key,
            )

    async def acquire(self):
        self.lock_value = str(uuid.uuid4())
        self._released = False
        # Clean up any leftover watchdog from a previous acquire on this
        # instance (e.g. if release() was not called or failed part-way).
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        while True:
            success = await self.store.exclusive_set(
                self.lock_key, self.lock_value, expiry=self.ttl
            )
            if success:
                # Start watchdog for auto-renewal
                self._watchdog_task = asyncio.create_task(self._watchdog())
                return True
            await asyncio.sleep(self.retry_delay)

    async def release(self):
        # Stop watchdog *before* setting _released so there is no race
        # between cancel and the watchdog's sleep loop.
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        self._released = True
        try:
            lock_key = await self.store.get(self.lock_key)
            if lock_key == self.lock_value:
                await self.store.delete(self.lock_key)
        except Exception as e:
            memory_logger.error(
                "Error releasing lock",
                exception=str(e),
                event_type=LogEventType.MEMORY_STORE,
            )

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()
