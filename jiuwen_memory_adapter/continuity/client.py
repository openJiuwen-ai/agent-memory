"""Remote-only transport shared by import scripts and product MCP bridges."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

VERBS = frozenset({"role_task", "role_record", "role_relation", "role_view", "role_rebuild", "get"})


class ContinuityClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        url = urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if (url.scheme != "https" and not (url.scheme == "http" and loopback)) or url.username:
            raise ValueError("continuity requires HTTPS (HTTP allowed only on loopback)")
        if not api_key:
            raise ValueError("a server-issued credential is required")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60, connect=5),
            transport=transport,
            follow_redirects=False,
        )

    def call(self, verb: str, payload: dict) -> dict:
        if verb not in VERBS:
            raise ValueError("unsupported continuity operation")
        try:
            response = self._http.post(f"/v1/{verb}", json=payload)
            if response.status_code >= 400:
                return {
                    "status": "rejected" if response.status_code < 500 else "unknown",
                    "error": f"http_{response.status_code}",
                }
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("expected structured response")
            return data
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "error": "transport_unavailable"}

    def close(self):
        self._http.close()
