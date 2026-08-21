"""MCP stdio / Streamable HTTP 凭据载体归一。"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_MCP_DIR = os.path.join("bootstrap", "mcp_server")
_CORE_DIR = os.path.join("bootstrap", "core")
for _path in (_MCP_DIR, _CORE_DIR, "src"):
    if _path not in sys.path:
        sys.path.append(_path)

from transport_security import credentials_for_transport, is_http_transport  # noqa: E402

from jiuwen_memory.common.errors import ValidationError  # noqa: E402

pytestmark = pytest.mark.unit


def _context(headers: dict[str, str], client: object) -> object:
    request = SimpleNamespace(headers=headers, client=client)
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def test_stdio_reads_the_shared_api_key_environment_variable() -> None:
    credentials = credentials_for_transport(
        "stdio", environ={"AGENT_MEMORY_API_KEY": "  stdio-key  "}
    )
    assert credentials.api_key == "stdio-key"
    assert credentials.headers == {}
    assert credentials.peer_address == ""


def test_streamable_http_reads_bearer_and_socket_peer() -> None:
    credentials = credentials_for_transport(
        "streamable-http",
        context=_context(
            {"Authorization": "Bearer http-key"}, SimpleNamespace(host="203.0.113.7")
        ),
        environ={"AGENT_MEMORY_API_KEY": "must-not-be-used"},
    )
    assert credentials.api_key == "http-key"
    assert credentials.peer_address == "203.0.113.7"
    assert credentials.headers["authorization"] == "Bearer http-key"


def test_http_alias_and_tuple_peer_are_supported() -> None:
    credentials = credentials_for_transport(
        "http", context=_context({"X-API-Key": "fallback-key"}, ("127.0.0.1", 9000))
    )
    assert is_http_transport("http")
    assert credentials.api_key == "fallback-key"
    assert credentials.peer_address == "127.0.0.1"


def test_streamable_http_never_falls_back_to_process_environment() -> None:
    with pytest.raises(ValidationError, match="request context unavailable"):
        credentials_for_transport(
            "streamable-http",
            context=None,
            environ={"AGENT_MEMORY_API_KEY": "wrong-security-boundary"},
        )


def test_unavailable_fastmcp_context_fails_closed() -> None:
    class _UnavailableContext:
        @property
        def request_context(self):
            raise ValueError("outside request")

    with pytest.raises(ValidationError, match="request context unavailable"):
        credentials_for_transport("streamable-http", context=_UnavailableContext())
