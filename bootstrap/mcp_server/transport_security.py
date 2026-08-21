"""MCP 传输层凭据提取。

stdio 没有 HTTP 请求头，凭据来自进程环境；Streamable HTTP 必须逐请求读取真实
``Authorization`` header 与 socket peer。这里只做协议归一，不认证、不产生身份。

接口先行版：本模块只固定 ``credentials_for_transport`` 的接缝，尚未接入
``mcp_server`` 的实际 Server lifecycle（随实装 PR 落地）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from auth_middleware import credentials_from_headers

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.types import Credentials

_HTTP_TRANSPORTS = frozenset({"http", "streamable-http"})
_API_KEY_ENV = "AGENT_MEMORY_API_KEY"


def is_http_transport(transport: str) -> bool:
    """是否为有网络对端的 MCP Streamable HTTP 传输。"""
    return transport.strip().lower() in _HTTP_TRANSPORTS


def credentials_for_transport(
    transport: str,
    *,
    context: Any = None,
    environ: Mapping[str, str] | None = None,
) -> Credentials:
    """把当前 MCP 传输携带的材料归一成协议无关 ``Credentials``。

    - stdio：读取 ``AGENT_MEMORY_API_KEY``；
    - Streamable HTTP：从 FastMCP Context 中的 Starlette Request 读取 headers 与 peer。

    HTTP 模式拿不到逐请求上下文属于接线错误，必须 fail-closed，不能回退到进程环境变量。
    """
    if not is_http_transport(transport):
        source = os.environ if environ is None else environ
        return Credentials(api_key=str(source.get(_API_KEY_ENV, "")).strip())

    request = _http_request(context)
    if request is None:
        raise ValidationError("MCP Streamable HTTP request context unavailable")
    return credentials_from_headers(request.headers, _peer_address(request))


def _http_request(context: Any) -> Any:
    if context is None:
        return None
    try:
        request_context = context.request_context
    except (AttributeError, ValueError):
        return None
    request = getattr(request_context, "request", None)
    if request is None or not hasattr(request, "headers"):
        return None
    return request


def _peer_address(request: Any) -> str:
    client = getattr(request, "client", None)
    if client is None:
        return ""
    host = getattr(client, "host", None)
    if host is not None:
        return str(host).strip()
    if isinstance(client, (tuple, list)) and client:
        return str(client[0]).strip()
    return ""
