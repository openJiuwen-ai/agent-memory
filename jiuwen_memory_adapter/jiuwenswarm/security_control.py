"""Shared control plane for a single local Swarm instance."""

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time
from uuid import uuid4

from ..agent_security._validation import has_exact_type
from ..agent_security.runtime import SecurityDenied
from .security_policy import SwarmPolicySettings, category_catalog

PROTOCOL_VERSION = 1
_HEARTBEAT_SECONDS = 2
_STALE_SECONDS = 10
logger = logging.getLogger(__name__)


class StateConflict(RuntimeError):
    """The control state changed since it was read."""


class SwarmProtectionStore:
    """Gateway writes intent, the AgentServer Worker acknowledges actual admission."""

    def __init__(self, path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS desired (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO desired VALUES (1, 0, 0);
                CREATE TABLE IF NOT EXISTS policy_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    worker TEXT NOT NULL, updated REAL NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    revision INTEGER NOT NULL
                );
            """)

    def _connect(self):
        return sqlite3.connect(self._path, timeout=2)

    def desired(self):
        with closing(self._connect()) as conn:
            enabled, revision = conn.execute(
                "SELECT enabled, revision FROM desired WHERE id = 1"
            ).fetchone()
        return bool(enabled), revision

    @staticmethod
    def _policy(value):
        if value is None:
            return SwarmPolicySettings.defaults()
        settings = json.loads(value)
        # Earlier local settings supported two outlets and per-outlet approval options.
        if has_exact_type(settings, dict):
            settings.setdefault("agents", True)
            for key in ("model", "tools"):
                setting = settings.get(key)
                if has_exact_type(setting, dict) and "enabled" in setting:
                    settings[key] = setting["enabled"]
        return SwarmPolicySettings.parse(settings)

    def snapshot(self):
        with closing(self._connect()) as conn:
            enabled, revision, policy = conn.execute(
                "SELECT d.enabled, d.revision, p.value FROM desired d "
                "LEFT JOIN policy_settings p ON p.id=d.id WHERE d.id=1"
            ).fetchone()
        return bool(enabled), revision, self._policy(policy)

    def set_enabled(self, enabled, revision):
        if not has_exact_type(enabled, bool) or not has_exact_type(revision, int) or revision < 0:
            raise ValueError("invalid protection update")
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                "UPDATE desired SET enabled = ?, revision = revision + 1 WHERE id = 1 "
                "AND revision = ?",
                (enabled, revision),
            )
            if updated.rowcount != 1:
                raise StateConflict("protection setting changed")
        return self.public()

    def set_policy(self, value, revision):
        if not has_exact_type(revision, int) or revision < 0:
            raise ValueError("invalid protection revision")
        policy = SwarmPolicySettings.parse(value)
        payload = json.dumps(policy.mapping(), separators=(",", ":"))
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                "UPDATE desired SET revision=revision+1 WHERE id=1 AND revision=?",
                (revision,),
            )
            if updated.rowcount != 1:
                raise StateConflict("protection setting changed")
            conn.execute(
                "INSERT INTO policy_settings VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value", (payload,),
            )
        return self.public()

    def publish(self, worker, snapshot):
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                "INSERT INTO runtime VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET worker=excluded.worker, updated=excluded.updated, "
                "enabled=excluded.enabled, revision=excluded.revision "
                "WHERE runtime.worker=excluded.worker OR runtime.updated < ?",
                (worker, time(), *snapshot, time() - _STALE_SECONDS),
            )
            if updated.rowcount != 1:
                raise StateConflict("a Swarm Worker already owns the protection state")

    def release(self, worker):
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM runtime WHERE id = 1 AND worker = ?", (worker,))

    def public(self):
        with closing(self._connect()) as conn:
            desired, revision, enabled, applied, updated, policy = conn.execute(
                "SELECT d.enabled, d.revision, r.enabled, r.revision, r.updated, p.value "
                "FROM desired d LEFT JOIN runtime r ON r.id=d.id "
                "LEFT JOIN policy_settings p ON p.id=d.id WHERE d.id=1"
            ).fetchone()
        live = updated is not None and 0 <= time() - updated <= _STALE_SECONDS
        return {
            "schema": PROTOCOL_VERSION,
            "desired": bool(desired),
            "revision": revision,
            "policy": self._policy(policy).mapping(),
            "categories": category_catalog(),
            "effective": bool(enabled) if live else None,
            "status": (
                "unavailable" if not live else "pending" if applied != revision
                else "enabled" if enabled else "disabled"
            ),
            "configuration": {
                "memory_backend": "jiuwen",
                "memory_writeback": False,
                "model_protocol": "openai-chat",
                "tool_scope": "authorized-text-mcp",
                "approval": "original-conversation",
            } if live else None,
        }


class SwarmProtectionRuntime:
    """Run in the Worker loop that owns admission. Existing sessions keep their mode."""

    def __init__(self, store):
        self._store = store
        self._worker = uuid4().hex
        self._active = None
        self._task = None
        self._stop = asyncio.Event()

    @property
    def enabled(self):
        if self._stop.is_set() or self._active is None:
            raise SecurityDenied("Swarm 保护状态不可用")
        if self._task is None or self._task.done():
            raise SecurityDenied("Swarm 保护状态不可用")
        return self._active[0]

    @property
    def policy(self):
        if not self.enabled:
            raise SecurityDenied("新会话保护已关闭")
        return self._active[2]

    async def start(self, ready):
        if self._task is not None or self._stop.is_set():
            raise RuntimeError("Swarm protection runtime already started")
        if not ready():
            raise SecurityDenied("Swarm 保护要求个人版 harness 和本地记忆")
        await self._refresh_state(ready)
        self._task = asyncio.create_task(self._refresh(ready))

    async def _refresh_state(self, ready):
        try:
            if ready():
                snapshot = await asyncio.to_thread(self._store.snapshot)
                await asyncio.to_thread(self._store.publish, self._worker, snapshot[:2])
                self._active = snapshot
            else:
                self._active = None
                await asyncio.to_thread(self._store.release, self._worker)
        except (StateConflict, ValueError, sqlite3.Error) as exc:
            self._active = None
            logger.warning("Swarm protection heartbeat unavailable (%s)", type(exc).__name__)

    async def _refresh(self, ready):
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    pass
                if self._stop.is_set():
                    return
                await self._refresh_state(ready)
        finally:
            self._active = None

    async def aclose(self):
        self._stop.set()
        if self._task is not None:
            # Let any in-progress SQLite write finish before withdrawing the receipt.
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await asyncio.to_thread(self._store.release, self._worker)
