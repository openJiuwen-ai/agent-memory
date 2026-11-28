"""Remote-only transport shared by import scripts and product MCP bridges."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

VERBS = frozenset(
    {
        "continuity_task",
        "continuity_relation",
        "continuity_recover",
        "batch_add",
        "add",
        "search",
        "get",
    }
)


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

    def call(self, verb: str, payload: dict):
        if verb not in VERBS:
            raise ValueError("unsupported continuity operation")
        try:
            response = self._http.post(f"/v1/{verb}", json=payload)
            if response.status_code >= 400:
                return _error_result(response)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return {"status": "unknown", "error": "transport_unavailable"}

    def close(self):
        self._http.close()


def _error_result(response) -> dict:
    """Preserve a 409 write receipt instead of flattening it to "rejected" (F17 D8)."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    result = {"error": f"http_{response.status_code}"}
    if body.get("message"):
        result["message"] = body["message"]
    if response.status_code >= 500:
        result["status"] = "unknown"
        return result
    receipt_status = body.get("status")
    if response.status_code == 409 and receipt_status in {"partial", "unknown"}:
        result["status"] = receipt_status
        for key in ("operation_id", "refs", "retry_action"):
            if key in body:
                result[key] = body[key]
        return result
    result["status"] = "rejected"
    return result
