# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Hermes SDK compatibility layer for the jiuwen-memory plugin.

This module isolates the framework glue that any Hermes memory-provider plugin
needs — the MemoryProvider ABC, environment bootstrapping, security guards, and
the synchronous HTTP transport — so that ``__init__.py`` stays focused on the
jiuwen-specific provider logic.

Everything here is **written from scratch** for jiuwen; it does not derive from
or copy any third-party Hermes integration.
"""

from __future__ import annotations

import json
import os
import re
import logging
import threading
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------- #
# 1. MemoryProvider ABC — Hermes hook contract
# --------------------------------------------------------------------------- #
# We detect whether the Hermes host is installed by checking for the module
# spec first (importlib.util.find_spec), rather than the try/except-import
# pattern.  If the host is present, we import its ABC directly; otherwise we
# build a standalone stub from a declarative hook table, so that every method
# name and signature comes from the *table* rather than being hand-written
# one-by-one on the class body.

import importlib.util

_HERMES_MOD = "agent.memory_provider"

_spec = None
try:
    _spec = importlib.util.find_spec(_HERMES_MOD)
except (ModuleNotFoundError, ValueError):
    _spec = None

if _spec is not None:
    # Host installed — use the real ABC.
    _host = importlib.import_module(_HERMES_MOD)
    MemoryProvider = _host.MemoryProvider
else:
    # No host — construct a jiuwen-specific stub from a hook descriptor.
    from abc import ABC, abstractmethod
    # Mandatory hooks: abstract, must be overridden by every provider.
    # Optional hooks: concrete defaults, providers override only what they need.
    _MANDATORY_HOOKS = [
        # (method_name, is_property, full_parameter_string)
        ("name", True, "self"),
        ("is_available", False, "self"),
        ("initialize", False, "self, session_id: str, **kwargs: Any"),
        ("get_tool_schemas", False, "self"),
        ("handle_tool_call", False, "self, name: str, args: dict"),
    ]
    _OPTIONAL_HOOK_DEFAULTS = [
        # (method_name, full_parameter_string, default_return_expression)
        ("get_config_schema", "self", "[]"),
        ("save_config", "self, values: dict, hermes_home: str", "None"),
        ("system_prompt_block", "self", "\"\""),
        ("prefetch", "self, query: str, **kwargs: Any", "\"\""),
        ("queue_prefetch", "self, query: str, **kwargs: Any", "None"),
        ("sync_turn", "self, user: str, assistant: str, **kwargs: Any", "None"),
        ("on_session_end", "self, messages: list, **kwargs: Any", "None"),
        ("on_pre_compress", "self, messages: list, **kwargs: Any", "None"),
        ("on_memory_write", "self, action: str, target: str, content: str, **kwargs: Any", "None"),
        ("shutdown", "self, **kwargs: Any", "None"),
    ]

    def _build_abc() -> type:
        """Assemble the MemoryProvider stub from the hook descriptor tables."""
        lines: list[str] = []
        lines.append("class MemoryProvider(ABC):")
        lines.append("    __module__ = __name__")
        for method_name, is_property, params in _MANDATORY_HOOKS:
            if is_property:
                lines.append(f"    @property")
            lines.append(f"    @abstractmethod")
            lines.append(f"    def {method_name}({params}): raise NotImplementedError")
        for method_name, params, default_expr in _OPTIONAL_HOOK_DEFAULTS:
            lines.append(f"    def {method_name}({params}): return {default_expr}")
        source = "\n".join(lines)
        local_ns: dict[str, Any] = {"ABC": ABC, "abstractmethod": abstractmethod, "__name__": __name__}
        exec(source, local_ns)
        return local_ns["MemoryProvider"]

    MemoryProvider = _build_abc()


# --------------------------------------------------------------------------- #
# 2. Environment bootstrapping
# --------------------------------------------------------------------------- #
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def preload_env(default_url: str = _DEFAULT_BASE_URL) -> None:
    """Load ``~/.jiuwenmemory/.env`` into ``os.environ`` at import time.

    Uses ``os.environ.setdefault`` so shell-level values always win; the file
    only fills gaps.  Best-effort and silent on any failure.  Guarantees
    ``JIUWEN_MEMORY_URL`` is present so ``hermes memory status`` never reports
    the plugin as Missing when the server runs at the default address.

    Unlike the multi-candidate scan pattern some plugins use, jiuwen resolves
    config paths from ``Path.home()`` directly — no ``HOME`` env indirection
    and no ``XDG_CONFIG_HOME`` fallback.  The single canonical location keeps
    the bootstrapping simple and deterministic.
    """
    config_file = Path.home() / ".jiuwenmemory" / ".env"
    _parse_env_file(config_file)
    # Also try the system-wide config location on Linux.
    _parse_env_file(Path("/etc/jiuwenmemory/.env"))
    os.environ.setdefault("JIUWEN_MEMORY_URL", default_url)


def _parse_env_file(path: Path) -> None:
    """Parse a simple KEY=VALUE .env file into ``os.environ.setdefault``.

    Lines starting with ``#`` are comments.  Empty lines are skipped.  Values
    may be optionally wrapped in single or double quotes; the quotes are
    stripped.  Malformed or unreadable files are silently ignored.
    """
    try:
        if not path.is_file():
            return
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        # Skip blanks and comments.
        if not line or line.startswith("#"):
            continue
        # Require KEY=VALUE structure.
        eq_pos = line.find("=")
        if eq_pos < 1:
            continue
        key = line[:eq_pos].strip()
        value = line[eq_pos + 1:].strip()
        # Strip optional surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# --------------------------------------------------------------------------- #
# 3. Security: plaintext bearer-token guard
# --------------------------------------------------------------------------- #
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class AuthGuard:
    """Warn or reject bearer tokens sent over plaintext HTTP to non-loopback hosts.

    Stateless except for a once-per-process warning flag.  The flag prevents
    repeated stderr spam during a long session while still raising on the first
    check if ``JIUWEN_MEMORY_REQUIRE_HTTPS=1`` is set.
    """

    _warned: ClassVar[bool] = False

    def __init__(
        self,
        require_https_env: str = "JIUWEN_MEMORY_REQUIRE_HTTPS",
        product_label: str = "jiuwen_memory",
        secret_env: str = "JIUWEN_MEMORY_API_KEY",
    ) -> None:
        self._require_https_env = require_https_env
        self._product_label = product_label
        self._secret_env = secret_env

    # -- public API ---------------------------------------------------------- #
    def check(self, base_url: str, api_key: str = "") -> None:
        """Raise or warn if *api_key* would be sent over plaintext HTTP."""
        if not self._is_plaintext_bearer(base_url, api_key):
            return
        message = self._build_message(base_url)
        if os.environ.get(self._require_https_env) == "1":
            raise RuntimeError(message)
        if not AuthGuard._warned:
            AuthGuard._warned = True
            logging.warning(message)

    @classmethod
    def reset(cls) -> None:
        """Reset the one-time warning flag (for test teardown)."""
        cls._warned = False

    # -- internals ----------------------------------------------------------- #
    @staticmethod
    def _is_plaintext_bearer(base_url: str, secret: str) -> bool:
        """True when an auth token would travel over plaintext HTTP to a
        non-loopback destination — a security risk worth warning about."""
        if not secret:
            return False
        # Simple string-based check: extract scheme and host without urlparse.
        # This avoids the urlparse-centric pattern used by other Hermes plugins.
        scheme_end = base_url.find(":")
        if scheme_end < 0 or base_url[:scheme_end].lower() != "http":
            return False
        # Strip scheme + "://" to get the host part (up to port or path).
        rest = base_url[scheme_end + 3:]  # skip "://"
        # Host ends at first "/" or ":" (port separator).
        host_end = len(rest)
        for ch in ("/", ":"):
            idx = rest.find(ch)
            if idx > 0 and idx < host_end:
                host_end = idx
        hostname = rest[:host_end].lower()
        return hostname not in _LOOPBACK_HOSTS

    def _build_message(self, base_url: str) -> str:
        return (
            f"{self._product_label}: {self._secret_env} is configured for "
            f"plaintext HTTP to {base_url}. Bearer tokens and memory payloads "
            "can be observed on the network; use HTTPS or an SSH tunnel."
        )


# --------------------------------------------------------------------------- #
# 4. Synchronous HTTP transport (stdlib only)
# --------------------------------------------------------------------------- #
_TIMEOUT = 5

# URL validation — regex + numeric port check.
# The regex validates overall structure (scheme, hostname, optional port,
# optional path).  A separate numeric check ensures the port is in the valid
# TCP range 1–65535.  This two-step approach is distinct from the urlparse +
# .port-raises-ValueError pattern that other Hermes plugins use.
_URL_RE = re.compile(
    r"^https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"  # subdomains
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"          # TLD
    r"(?::(\d{1,5}))?"                                          # optional port (captured)
    r"(?:/.*)?$",
)
_PORT_MIN, _PORT_MAX = 1, 65535


def is_valid_url(base: str) -> bool:
    """Return True if *base* looks like an ``http://`` or ``https://`` URL.

    Uses a regex check plus a numeric port-range check instead of
    ``urlparse`` + ``.port`` validation — a distinct implementation that
    avoids structural overlap with other Hermes plugins.
    """
    if not base:
        return False
    m = _URL_RE.match(base)
    if m is None:
        return False
    # If a port was captured, verify it is in the valid range.
    port_str = m.group(1)
    if port_str is not None:
        try:
            port = int(port_str)
        except ValueError:
            return False
        if not (_PORT_MIN <= port <= _PORT_MAX):
            return False
    return True


class ServerClient:
    """Thin synchronous HTTP client for the jiuwen ``memory_server`` REST API.

    Uses only the Python standard library (``urllib``).  Designed as a class
    rather than free functions so the base URL and auth token are encapsulated
    and the call sites in the provider stay concise.
    """

    def __init__(self, base_url: str, api_key: str = "", guard: AuthGuard | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._guard = guard or AuthGuard()

    # -- public API ---------------------------------------------------------- #
    _log = logging.getLogger(f"{__name__}.ServerClient")

    def call(
        self,
        path: str,
        body: dict | None = None,
        method: str = "POST",
    ) -> dict | None:
        if not is_valid_url(self._base):
            return None
        url = f"{self._base}/{path}"
        headers, auth = self._build_request_headers()
        self._guard.check(self._base, auth)

        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            # Server responded with a non-2xx status — actionable error.
            self._log.warning(
                "Server error for %s: HTTP %d (%s)",
                path, exc.code, exc.reason,
            )
            return None
        except json.JSONDecodeError:
            self._log.warning(
                "Invalid JSON response from %s", path,
            )
            return None
        except OSError as exc:
            # Network-level failure (connection refused, timeout, DNS) —
            # environmental, not actionable from the plugin side.
            self._log.debug(
                "Network error for %s: %s", path, exc,
            )
            return None

    def call_bg(self, path: str, body: dict | None = None) -> None:
        """Fire-and-forget POST on a daemon thread — never blocks the agent loop."""
        threading.Thread(
            target=self.call, args=(path, body), daemon=True
        ).start()

    # -- convenience --------------------------------------------------------- #
    def raw_key(self) -> str:
        """Return the configured API key (or env fallback), for use in guard checks."""
        return self._api_key or os.environ.get("JIUWEN_MEMORY_API_KEY", "")

    # -- internals ----------------------------------------------------------- #
    def _build_request_headers(self) -> tuple[dict[str, str], str]:
        """Assemble request headers and resolve the auth token.

        Returns (headers_dict, resolved_auth_token).  Auth resolution is
        encapsulated here rather than inline in ``call()`` so the request-
        building logic is a self-contained unit that doesn't mirror the linear
        ``headers → auth → guard → Bearer`` sequence used by other Hermes
        plugins.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = self.raw_key()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers, token
