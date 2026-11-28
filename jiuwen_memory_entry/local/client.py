# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Minimal standard-library client for the local service's ``/local/*`` endpoints."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from jiuwen_memory_entry.local.discovery import Discovery, ServiceUnavailable


class ServiceClient:
    """Bearer-token client bound to one discovery entry."""

    def __init__(self, discovery: Discovery, token: str, *, timeout: float = 10.0) -> None:
        self._discovery = discovery
        self._token = token
        self._timeout = timeout
        self._base = f"http://127.0.0.1:{discovery.port}"

    @property
    def discovery(self) -> Discovery:
        return self._discovery

    def post(
        self, path: str, payload: dict, *, timeout: float | None = None
    ) -> tuple[int, dict]:
        return self._call("POST", path, payload, timeout=timeout)

    def status(self) -> dict:
        status, body = self._call("GET", "/local/status")
        if status != 200:
            raise ServiceUnavailable(f"status failed: HTTP {status}")
        return body

    def health(self) -> dict:
        status, body = self._call("GET", "/local/health", authenticated=False)
        if status != 200:
            raise ServiceUnavailable(f"health failed: HTTP {status}")
        return body

    def egress(self) -> list[dict]:
        status, body = self._call("GET", "/local/egress")
        if status != 200:
            raise ServiceUnavailable(f"egress failed: HTTP {status}")
        return list(body.get("components") or [])

    def lease(self, client_id: str) -> None:
        status, body = self._call("POST", "/local/lease", {"client_id": client_id})
        if status != 200:
            raise ServiceUnavailable(f"lease failed: HTTP {status} {body.get('error', '')}")

    def release(self, client_id: str) -> None:
        status, _body = self._call("DELETE", "/local/lease", {"client_id": client_id})
        if status not in (200, 204, 404):
            raise ServiceUnavailable(f"lease release failed: HTTP {status}")

    def reload(self) -> None:
        status, body = self._call("POST", "/local/reload", {})
        if status not in (200, 202):
            raise ServiceUnavailable(f"reload failed: HTTP {status} {body.get('error', '')}")

    def _call(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float | None = None,
        authenticated: bool = True,
    ) -> tuple[int, dict]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            return exc.code, body
        except (OSError, ValueError) as exc:
            raise ServiceUnavailable(f"memory service is unreachable: {exc}") from exc
