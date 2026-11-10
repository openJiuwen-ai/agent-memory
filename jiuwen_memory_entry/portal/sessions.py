# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Browser launch grants, sessions and opt-in iframe policy shared by HTTP and SDK."""

from __future__ import annotations

import re
import secrets
import time
from http.cookies import SimpleCookie
from threading import Lock
from urllib.parse import urlsplit

from jiuwen_memory.api import ValidationError

SESSION_HEADER = "X-Memory-Portal-Session"
BROWSER_HEADER = "X-Memory-Portal-Browser"
_MAX_ENTRIES = 32
_ORIGIN = re.compile(r"https?://(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::[0-9]+)?")


def frame_origins(values) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        raise ValidationError("Portal frame origins must be a list of HTTP(S) origins")
    for value in values:
        if not isinstance(value, str) or not _ORIGIN.fullmatch(value):
            raise ValidationError("Portal frame origins must be exact HTTP(S) origins")
        try:
            origin = urlsplit(value)
            if origin.port == 0:
                raise ValueError
        except ValueError as error:
            raise ValidationError("invalid Portal frame origin") from error
    return tuple(dict.fromkeys(values))


def frame_headers(origins: tuple[str, ...]) -> dict[str, str]:
    if not origins:
        return {"X-Frame-Options": "DENY"}
    return {"Content-Security-Policy": "frame-ancestors " + " ".join(origins)}


def bootstrap(sessions, body, origins):
    """Use a page-local token in iframes, where cookies may be blocked."""
    if not isinstance(body, dict):
        return None, None
    embedded = body.get("embedded", False)
    if not isinstance(embedded, bool) or (embedded and not origins):
        return None, None
    session = sessions.redeem(body.get("token"), embedded=embedded)
    if session is None:
        return None, None
    if embedded:
        return {"ready": True, "session_token": session}, None
    return {"ready": True}, session


class BrowserSessions:
    def __init__(self):
        self.lock = Lock()
        self.grants = {}
        self.sessions = {}
        self.cookie = "memory_portal_" + secrets.token_hex(8)

    def _prune(self):
        now = time.monotonic()
        for table in (self.grants, self.sessions):
            for key in list(table):
                if table[key][0] <= now:
                    del table[key]

    def issue(self, payload=None):
        with self.lock:
            self._prune()
            if len(self.grants) >= _MAX_ENTRIES:
                del self.grants[next(iter(self.grants))]
            token = secrets.token_urlsafe(32)
            self.grants[token] = (time.monotonic() + 300, payload)
            return token

    def redeem(self, token, *, embedded=False):
        if not isinstance(token, str):
            return None
        with self.lock:
            self._prune()
            grant = self.grants.pop(token, None)
            if grant is None:
                return None
            if len(self.sessions) >= _MAX_ENTRIES:
                del self.sessions[next(iter(self.sessions))]
            session = secrets.token_urlsafe(32)
            self.sessions[session] = (time.monotonic() + 3600, grant[1], embedded)
            if embedded:
                return session
            return (
                f"{self.cookie}={session}; Path=/v1/; HttpOnly; SameSite=Strict; Max-Age=3600"
            )

    def _read_cookie(self, header):
        cookie = SimpleCookie()
        try:
            cookie.load(header or "")
        except Exception:
            return None
        value = cookie.get(self.cookie)
        return value.value if value is not None else None

    def has_cookie(self, header):
        return self._read_cookie(header) is not None

    def lookup(self, header, *, token=None):
        embedded = token is not None
        if not embedded:
            token = self._read_cookie(header)
            if token is None:
                return False, None
        with self.lock:
            self._prune()
            entry = self.sessions.get(token)
            if entry is None or entry[2] != embedded:
                return False, None
            return True, entry[1]

    def close(self):
        with self.lock:
            self.grants.clear()
            self.sessions.clear()
