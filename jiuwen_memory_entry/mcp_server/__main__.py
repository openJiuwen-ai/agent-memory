# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP surface 入口——基于 FastMCP，把记忆动词暴露为 MCP 工具。

每个工具都是对**共享 ``handler.dispatch``** 的薄封装（HTTP / CLI 用的是同一个 dispatch），
故本适配器只做协议翻译、零业务逻辑。内核进程内装配一次、跨工具调用持有状态。

启动（stdio 传输，供 Claude Desktop / Claude Code 等 MCP 客户端挂载）::

    pip install ".[mcp]"
    scripts/run-mcp.sh [config.yml ...]     # 缺省纯内存 OFFLINE 栈

或 Streamable HTTP::

    MCP_TRANSPORT=http MCP_PORT=8138 scripts/run-mcp.sh /config/config.yml

凭据通道与 CLI/HTTP 对齐：stdio 从 ``AGENT_MEMORY_API_KEY`` 读取 API Key；
Streamable HTTP 从逐请求 ``Authorization: Bearer <key>``（兼容 ``X-API-Key``）提取，
并把真实 socket peer 交给共享限流、并发预算与认证链。surface 只提取原始材料，
身份仍只能由配置选定的 Authenticator 产生。
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module

# MCP 工具函数的参数就是已发布的工具 schema；封装成 dataclass 会破坏对外契约。
# pylint: disable=huawei-too-many-arguments

# 复用 jiuwen_memory_entry/core 的共享件（server 内核装配 + profiles 配置叠加 + handler dispatch +
# config_loader 配置加载），与 CLI 相同的 flat-import；启动脚本通过 PYTHONPATH 保证优先级，
# 这里 append 仅作为直接运行本文件时的兜底。
_BOOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_BOOT)
_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_BOOT, "core"), _REPO, _MCP_DIR):
    if _p not in sys.path:
        sys.path.append(_p)

load_layer = import_module("config_loader").load_layer
_profiles_module = import_module("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_module("server").Server

_auth_middleware = import_module("auth_middleware")
authenticated = _auth_middleware.authenticated
Surface = import_module("jiuwen_memory.common.security.types").Surface
AuthenticationError = import_module("jiuwen_memory.common.errors").AuthenticationError
ValidationError = import_module("jiuwen_memory.common.errors").ValidationError

_transport_security = import_module("transport_security")
credentials_for_transport = _transport_security.credentials_for_transport
is_http_transport = _transport_security.is_http_transport

try:
    _fastmcp = import_module("mcp.server.fastmcp")
    FastMCP = _fastmcp.FastMCP
    Context = _fastmcp.Context
except ImportError as import_error:  # pragma: no cover
    raise RuntimeError(
        'MCP surface 需要 mcp SDK：pip install ".[mcp]"（或 pip install mcp）'
    ) from import_error

# --- 内核：进程内装配一次，跨工具调用共享 --- #
_SRV = Server.build(load_config([OFFLINE] + [load_layer(p) for p in sys.argv[1:]]))

mcp = FastMCP(
    "agent-memory",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8138")),
)


def _call(verb: str, payload: dict, context: Context) -> dict:
    """
    走共享 dispatch；非 2xx 抛错，让 MCP 客户端看到失败原因（None 入参不下发）。

    凭据只从当前传输的可信载体提取：stdio 读进程环境，Streamable HTTP 读逐请求
    header 与 socket peer。PR1 仍由共享 dispatch 从认证 ContextVar 取身份；PR2 再把
    认证结果改为显式 ``RequestSecurityContext`` 参数交给唯一 PEP。
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    credentials = credentials_for_transport(transport, context=context)
    networked = is_http_transport(transport)
    try:
        with authenticated(
            _SRV.authenticator,
            credentials,
            _SRV.audit,
            _SRV.rate_limiter if networked else None,
            workload_guard=_SRV.workload_guard if networked else None,
            surface=Surface.MCP,
        ):
            status, body = _SRV.dispatch(verb, {k: v for k, v in payload.items() if v is not None})
    except AuthenticationError as auth_error:
        raise RuntimeError(f"{type(auth_error).__name__}: {auth_error}") from auth_error
    if status >= 400:
        raise RuntimeError(f"{body.get('error', 'Error')}: {body.get('message', '')}")
    return body


# --- 工具：记忆生命周期（与 handler 的 verb 一一对应，签名带类型生成 JSON Schema） --- #


@mcp.tool()
def memory_add(
    tenant_id: str,
    scope: str,
    content: str,
    ctx: Context,
    tags: list[str] | None = None,
) -> dict:
    """
    写入一条记忆。tenant_id=租户，scope=归属（用户/会话），content=内容，tags=可选标签。
    """
    return _call(
        "add", {"tenant_id": tenant_id, "scope": scope, "content": content, "tags": tags}, ctx
    )


@mcp.tool()
def memory_search(
    tenant_id: str,
    scope: str,
    query: str,
    ctx: Context,
    k: int = 5,
    trace: bool = False,
) -> dict:
    """按「语义 + 关键词」双路召回记忆。k=返回条数；trace=true 时附带检索轨迹。"""
    return _call(
        "search",
        {
            "tenant_id": tenant_id,
            "scope": scope,
            "query": query,
            "k": k,
            "trace": trace,
        },
        ctx,
    )


@mcp.tool()
def memory_list(tenant_id: str, scope: str, ctx: Context) -> dict:
    """列出某 scope 下的全部记忆单元。"""
    return _call("list", {"tenant_id": tenant_id, "scope": scope}, ctx)


@mcp.tool()
def memory_get(tenant_id: str, scope: str, item_id: str, ctx: Context) -> dict:
    """按 id 读取单条记忆。"""
    return _call("get", {"tenant_id": tenant_id, "scope": scope, "item_id": item_id}, ctx)


@mcp.tool()
def memory_update(
    tenant_id: str,
    scope: str,
    item_id: str,
    ctx: Context,
    content: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """更新一条记忆的内容/标签（非破坏式，生成新版本并保留血缘）。"""
    return _call(
        "update",
        {
            "tenant_id": tenant_id,
            "scope": scope,
            "item_id": item_id,
            "content": content,
            "tags": tags,
        },
        ctx,
    )


@mcp.tool()
def memory_delete(
    tenant_id: str, scope: str, item_id: str, ctx: Context, hard: bool = False
) -> dict:
    """删除一条记忆。hard=false 软删（遗忘，记录仍可追溯）；true 物理清除。"""
    return _call(
        "delete",
        {"tenant_id": tenant_id, "scope": scope, "item_id": item_id, "hard": hard},
        ctx,
    )


@mcp.tool()
def memory_evolve(tenant_id: str, scope: str, ctx: Context, mode: str = "extract") -> dict:
    """触发记忆演进。mode=extract / associate / consolidate / forget，返回后台 job_id。"""
    return _call("evolve", {"tenant_id": tenant_id, "scope": scope, "mode": mode}, ctx)


def main() -> int:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http"):
        # Streamable HTTP 与 HTTP surface 走同一条 BindingPolicy；DEV 绑定非 loopback
        # 等于把 ROOT 级记忆工具暴露给整个网络。
        try:
            _SRV.binding_policy.check(
                os.environ.get("MCP_HOST", "127.0.0.1"),
                requires_loopback=_SRV.authenticator.requires_loopback_binding(),
            )
        except ValidationError as validation_error:
            logging.error("FATAL: %s", validation_error)
            return 1
        mcp.run(transport="streamable-http")  # host/port 已在 FastMCP(...) 设好
    else:
        mcp.run()  # stdio（默认）——Claude Desktop / Claude Code 直接挂载
    return 0


if __name__ == "__main__":
    sys.exit(main())
