# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Watchdog-based file watcher for FileMemoryIndex.

Monitors ``{root_dir}/memories/`` recursively for ``.md`` file changes
(create / modify / delete) and triggers incremental re-sync via callback.

If ``watchdog`` is not installed, the watcher gracefully degrades to a
no-op — the system falls back to lazy sync-on-search.

Design mirrors agent-core_6760's ``MemoryIndexManager._setup_file_watcher()``.

.. code-block:: python

    watcher = MemoryFileWatcher(root_dir, on_file_changed)
    watcher.start()
    # ... app runs ...
    watcher.stop()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Sync callback signature: async def(path: str, event_type: str) -> None
SyncCallback = Callable[[str, str], Awaitable[None]]

_WATCHDOG_AVAILABLE = False
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    pass


class MemoryFileWatcher:
    """Watches ``{root_dir}/memories/`` for .md file changes.

    When a change is detected, waits for a 2-second debounce period,
    then calls ``on_file_changed(path, event_type)`` for each pending file.

    If ``watchdog`` is not installed, ``start()`` is a no-op.
    """

    def __init__(self, root_dir: Path, on_file_changed: SyncCallback):
        self._root = root_dir
        self._on_file_changed = on_file_changed
        self._observer = None
        self._watch_timer: asyncio.Task | None = None
        # Pending events are only ever touched from the event loop thread
        # (see _enqueue_event via call_soon_threadsafe), so no lock is needed.
        self._pending: dict[str, str] = {}   # path → event_type
        self._debounce_s = 2.0
        self._initialized = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def available(self) -> bool:
        """``True`` if watchdog is installed and can be used."""
        return _WATCHDOG_AVAILABLE

    @property
    def running(self) -> bool:
        """``True`` if the observer is currently active."""
        return self._observer is not None and self._observer.is_alive()

    @property
    def debounce_s(self) -> float:
        """Debounce period in seconds (read-write — for testing)."""
        return self._debounce_s

    @debounce_s.setter
    def debounce_s(self, value: float) -> None:
        self._debounce_s = value

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop the watcher schedules on (read-write — for testing)."""
        return self._loop

    @loop.setter
    def loop(self, value: asyncio.AbstractEventLoop | None) -> None:
        self._loop = value

    @property
    def initialized(self) -> bool:
        """``True`` after the 1-second startup delay has elapsed (read-write)."""
        return self._initialized

    @initialized.setter
    def initialized(self, value: bool) -> None:
        """Override initialization flag (for testing — bypasses 1-second delay)."""
        self._initialized = value

    def enqueue_event(self, path: str, event_type: str) -> None:
        """Inject an event into the debounce queue (public — for testing).

        等价于 ``_enqueue_event`` 的公开入口。测试通过此方法模拟 watchdog
        文件事件，而非触达受保护成员 ``_enqueue_event``。
        """
        self._enqueue_event(path, event_type)

    def start(self) -> None:
        """Start watching ``{root_dir}/memories/`` recursively.

        A 1-second initialization delay prevents event storms on startup.
        No-op if watchdog is unavailable.
        """
        if not _WATCHDOG_AVAILABLE:
            logger.debug("watchdog not installed — file watcher disabled")
            return

        watch_dir = self._root / "memories"
        if not watch_dir.exists():
            watch_dir.mkdir(parents=True, exist_ok=True)

        self._loop = asyncio.get_running_loop()

        handler = _MemoryFileHandler(self._on_event)
        self._observer = Observer()
        self._observer.schedule(handler, str(watch_dir), recursive=True)
        self._observer.start()

        # 1-second startup delay — ignore events during init
        self._loop.call_later(1.0, self._mark_initialized)
        logger.debug("File watcher started on %s", watch_dir)

    def stop(self) -> None:
        """Stop watching and cancel any pending debounce timer."""
        if self._watch_timer:
            self._watch_timer.cancel()
            self._watch_timer = None
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
        self._initialized = False
        logger.debug("File watcher stopped")

    # ------------------------------------------------------------------
    # test helpers
    # ------------------------------------------------------------------

    def simulate_file_event(self, abs_path: str, event_type: str) -> None:
        """Simulate a watchdog file event for testing.

        Sets up the event loop and initialization flag, then calls
        ``_on_event`` directly — equivalent to what the watchdog thread
        would do via ``call_soon_threadsafe``, but runs on the calling
        thread so the caller must ``await asyncio.sleep(0)`` to let the
        loop process the enqueued callback.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if not self._initialized:
            self._initialized = True
        self._on_event(abs_path, event_type)

    def get_pending_paths(self) -> list[str]:
        """Return file paths with pending debounce events (for testing)."""
        return list(self._pending.keys())

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _mark_initialized(self) -> None:
        self._initialized = True

    def _on_event(self, path: str, event_type: str) -> None:
        """Called from the watchdog thread on any .md file event.

        Watchdog dispatches from a background thread, but ``_pending`` and the
        debounce task are owned by the event loop. Hand the event over with
        ``call_soon_threadsafe`` so all state mutation happens on the loop
        thread — no cross-thread dict access, no race on ``_pending`` or
        ``_watch_timer``.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue_event, path, event_type)
        except RuntimeError:
            # Loop closed while we were scheduling — nothing to do.
            pass

    def _enqueue_event(self, path: str, event_type: str) -> None:
        """Run on the event loop thread: record the event and reset the timer."""
        if not self._initialized:
            return
        self._pending[path] = event_type
        self._reset_timer()

    def _reset_timer(self) -> None:
        """Reset the debounce timer — collapse rapid events into one sync."""
        if self._watch_timer and not self._watch_timer.done():
            self._watch_timer.cancel()
        if self._loop:
            self._watch_timer = asyncio.ensure_future(self._debounced_sync())

    async def _debounced_sync(self) -> None:
        """Wait for the debounce period, then sync all pending files.

        在进入处理循环前将 ``_watch_timer`` 置 ``None``，让处理期间（for 循环
        内的 await）新到达的事件走 ``_reset_timer`` 时发现 timer 已空 → 另起
        一轮 debounce，而不是 ``cancel`` 当前批次——避免批次中剩余文件永久丢失
        （尤其是 delete 事件，丢了没有兜底机制，会产生幽灵索引）。
        """
        await asyncio.sleep(self._debounce_s)
        # ★ 核心：处理期间 _watch_timer 不指向自己，防自取消
        self._watch_timer = None
        batch = dict(self._pending)
        self._pending.clear()

        for path, event_type in batch.items():
            try:
                await self._on_file_changed(path, event_type)
            except asyncio.CancelledError:
                # stop() 主动取消是正常退出路径，记录后向上抛出
                logger.debug("Debounce sync cancelled while processing %s", path)
                raise
            except Exception:
                logger.debug(
                    "File watcher sync failed for %s (event=%s)",
                    path, event_type, exc_info=True,
                )


class _MemoryFileHandler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """watchdog event handler — filters to .md files only."""

    def __init__(self, callback: Callable[[str, str], None]):
        if _WATCHDOG_AVAILABLE:
            super().__init__()
        self._cb = callback

    def on_modified(self, event) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self._cb(event.src_path, "modified")

    def on_created(self, event) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self._cb(event.src_path, "created")

    def on_deleted(self, event) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            self._cb(event.src_path, "deleted")
