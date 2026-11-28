"""Hermes MemoryProvider: recall before a turn and capture the visible exchange."""

from __future__ import annotations

import json
import os
import uuid


class JiuwenMemoryProvider:
    """Small standard-library-only provider; failures never reach the host turn."""

    name = "jiuwen_memory"

    def __init__(
        self,
        *,
        home=None,
        install_id: str = "",
        profile: str = "",
        project: str = "",
    ) -> None:
        self._home = home
        self._install_id = install_id or os.environ.get("JIUWEN_MEMORY_INSTALL", "")
        self._profile = profile
        self._project = project
        self._turns: dict[str, str] = {}

    def _connection(self):
        from jiuwen_memory_entry.local import discovery
        from jiuwen_memory_entry.local.client import ServiceClient
        from jiuwen_memory_entry.local.home import MemoryHome, resolve_home

        home = MemoryHome(self._home) if self._home else resolve_home()
        install = self._install_id
        if not install:
            candidates = []
            for directory in home.installs_dir.glob("hermes-*"):
                if not directory.is_dir():
                    continue
                if self._profile:
                    try:
                        record = json.loads(
                            (directory / "install.json").read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        continue
                    if str(record.get("profile", "")) != self._profile:
                        continue
                candidates.append(directory.name)
            if len(candidates) != 1:
                return None
            install = candidates[0]
            self._install_id = install
        token_path = home.installs_dir / install / "token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        entry = discovery.live_discovery(home) or discovery.ensure_service(home, timeout=5.0)
        return ServiceClient(entry, token), install

    def _scope(self, kwargs: dict) -> dict:
        return {
            "org": str(kwargs.get("org") or "local"),
            "space": str(kwargs.get("project") or self._project or "default"),
        }

    def prefetch(self, query: str, **kwargs):
        session = str(kwargs.get("session_id") or kwargs.get("session") or "")
        turn_id = str(kwargs.get("turn_id") or uuid.uuid4().hex)
        self._turns[session] = turn_id
        try:
            connection = self._connection()
            if connection is None:
                return ""
            client, _install = connection
            status, body = client.post(
                "/local/hook/recall",
                {
                    "host_session_id": session,
                    "turn_id": turn_id,
                    "prompt": query,
                    "project": self._scope(kwargs)["space"],
                },
            )
            if status != 200:
                return ""
            view = body.get("view") or {}
            items = view.get("items") or []
            return "\n".join(
                str(item.get("content", "")) for item in items if item.get("content")
            )
        except Exception:  # noqa: BLE001 - host turn must not fail
            return ""

    def on_memory_write(self, target, content, action="add", **kwargs):
        """Mirror Hermes' own memory writes through the service, best-effort."""
        try:
            connection = self._connection()
            if connection is None:
                return None
            client, _install = connection
            client.post(
                "/local/sync/mirror",
                {
                    "profile": str(kwargs.get("profile") or "default"),
                    "target": str(target),
                    "content": str(content or ""),
                    "action": str(action),
                    "project": self._scope(kwargs)["space"],
                },
            )
        except Exception:  # noqa: BLE001 - host write must not fail
            return None
        return None

    def sync_turn(self, user, assistant, **kwargs):
        session = str(kwargs.get("session_id") or kwargs.get("session") or "")
        turn_id = self._turns.pop(session, "") or str(
            kwargs.get("turn_id") or uuid.uuid4().hex
        )
        try:
            connection = self._connection()
            if connection is None:
                return None
            client, _install = connection
            client.post(
                "/local/hook/capture",
                {
                    "host_session_id": session,
                    "turn_id": turn_id,
                    "prompt": str(user or ""),
                    "answer": str(assistant or ""),
                    "project": self._scope(kwargs)["space"],
                },
            )
        except Exception:  # noqa: BLE001 - capture must never break the host
            return None
        return None
