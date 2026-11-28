# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Browser launch grants and sessions shared by HTTP and SDK."""

from __future__ import annotations

import secrets
import time
from http.cookies import SimpleCookie
from threading import Lock

SESSION_HEADER = "X-Memory-Portal-Session"
BROWSER_HEADER = "X-Memory-Portal-Browser"
_MAX_ENTRIES = 32


def bootstrap(sessions, body, *, shared=False):
    """Use page-local tokens for iframes and for portals shared by several runtimes."""
    if not isinstance(body, dict):
        return None, None
    embedded = body.get("embedded", False)
    if not isinstance(embedded, bool):
        return None, None
    session, bound = sessions.redeem_for_page(body.get("token"), embedded=embedded or shared)
    if session is None:
        return None, None
    if embedded or shared or bound:
        response = {"ready": True, "session_token": session}
        if bound:
            response["experience_bound"] = True
        if shared:
            response["shared"] = True
        return response, None
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

    def issue(self, payload=None, *, page_bound=False):
        with self.lock:
            self._prune()
            if len(self.grants) >= _MAX_ENTRIES:
                del self.grants[next(iter(self.grants))]
            token = secrets.token_urlsafe(32)
            self.grants[token] = (time.monotonic() + 300, payload, page_bound)
            return token

    def redeem(self, token, *, embedded=False):
        session, _ = self.redeem_for_page(token, embedded=embedded)
        return session

    def redeem_for_page(self, token, *, embedded=False):
        if not isinstance(token, str):
            return None, False
        with self.lock:
            self._prune()
            grant = self.grants.pop(token, None)
            if grant is None:
                return None, False
            if len(self.sessions) >= _MAX_ENTRIES:
                del self.sessions[next(iter(self.sessions))]
            session = secrets.token_urlsafe(32)
            embedded = embedded or grant[2]
            self.sessions[session] = (time.monotonic() + 3600, grant[1], embedded)
            if embedded:
                return session, grant[2]
            return (
                f"{self.cookie}={session}; Path=/v1/; HttpOnly; SameSite=Strict; Max-Age=3600"
            ), False

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

    def revoke(self, matches):
        """Revoke both unused grants and established sessions matching a binding."""
        with self.lock:
            for table in (self.grants, self.sessions):
                for token, entry in tuple(table.items()):
                    if matches(entry[1]):
                        del table[token]

    def close(self):
        with self.lock:
            self.grants.clear()
            self.sessions.clear()
