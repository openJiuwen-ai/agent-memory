"""MCP surface 入口——基于 FastMCP，把记忆动词暴露为 MCP 工具。

每个工具都是对**共享 ``handler.dispatch``** 的薄封装（HTTP / CLI 用的是同一个 dispatch），
故本适配器只做协议翻译、零业务逻辑。内核进程内装配一次、跨工具调用持有状态。

启动（stdio 传输，供 Claude Desktop / Claude Code 等 MCP 客户端挂载）::

    pip install ".[mcp]"
    scripts/run-mcp.sh [config.yml ...]     # 缺省纯内存 OFFLINE 栈

或 Streamable HTTP::

    MCP_TRANSPORT=http MCP_PORT=8138 scripts/run-mcp.sh /config/config.yml

**第一期认证限制（务必知悉）**：MCP 协议自己的凭据传递机制（OAuth 2.1 资源服务器、
工具调用级的 token 下发）是第二期内容，本 surface 目前只把一个**空凭据**过认证中间件。
后果是：DEV 模式（缺省 OFFLINE 档）下所有工具照常可用；一旦配成 API_KEY / TRUSTED 模式，
**所有 MCP 工具调用都会失败**（认证失败）。这是有意的——
``docs/features/common/F04-security-interfaces-and-encryption.md``
§8.2「MCP 协议的攻击面」需要专门设计，在设计落地前，让 MCP 在生产模式下不可用，
好过让它无认证可用。
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module

# 复用 bootstrap/core 的共享件（server 内核装配 + profiles 配置叠加 + handler dispatch +
# config_loader 配置加载），与 CLI 相同的 flat-import；启动脚本通过 PYTHONPATH 保证优先级，
# 这里 append 仅作为直接运行本文件时的兜底。
_BOOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_BOOT)
for _p in (os.path.join(_BOOT, "core"), os.path.join(_REPO, "src")):
    if _p not in sys.path:
        sys.path.append(_p)

load_layer = import_module("config_loader").load_layer
_profiles_module = import_module("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_module("server").Server

_auth_middleware = import_module("auth_middleware")
authenticated = _auth_middleware.authenticated
AuthenticationError = import_module("common.errors").AuthenticationError
ValidationError = import_module("common.errors").ValidationError
Credentials = import_module("security.types").Credentials
check_dev_binding = import_module("security").check_dev_binding
AuthMode = import_module("security.types").AuthMode

try:
    FastMCP = import_module("mcp.server.fastmcp").FastMCP
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


def _call(verb: str, payload: dict) -> dict:
    """
    走共享 dispatch；非 2xx 抛错，让 MCP 客户端看到失败原因（None 入参不下发）。

    与 ``InProcessClient`` 同样过一个空 ``Credentials()``：MCP 尚无凭据通道（见模块
    docstring 的第一期限制）。DEV 模式下得到 ROOT，非 DEV 模式下这里就会抛认证失败。
    """
    try:
        with authenticated(_SRV.authenticator, Credentials(), _SRV.audit):
            status, body = _SRV.dispatch(verb, {k: v for k, v in payload.items() if v is not None})
    except AuthenticationError as auth_error:
        raise RuntimeError(
            f"{type(auth_error).__name__}: {auth_error}"
            "（MCP surface 尚未支持凭据传递，仅可在 DEV 模式下使用）"
        ) from auth_error
    if status >= 400:
        raise RuntimeError(f"{body.get('error', 'Error')}: {body.get('message', '')}")
    return body


# --- 工具：记忆生命周期（与 handler 的 verb 一一对应，签名带类型生成 JSON Schema） --- #


@mcp.tool()
def memory_add(tenant_id: str, scope: str, content: str, tags: list[str] | None = None) -> dict:
    """
    写入一条记忆。tenant_id=租户，scope=归属（用户/会话），content=内容，tags=可选标签。
    """
    return _call("add", {"tenant_id": tenant_id, "scope": scope, "content": content, "tags": tags})


@mcp.tool()
def memory_search(tenant_id: str, scope: str, query: str, k: int = 5, trace: bool = False) -> dict:
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
    )


@mcp.tool()
def memory_list(tenant_id: str, scope: str) -> dict:
    """列出某 scope 下的全部记忆单元。"""
    return _call("list", {"tenant_id": tenant_id, "scope": scope})


@mcp.tool()
def memory_get(tenant_id: str, scope: str, item_id: str) -> dict:
    """按 id 读取单条记忆。"""
    return _call("get", {"tenant_id": tenant_id, "scope": scope, "item_id": item_id})


@mcp.tool()
def memory_update(
    tenant_id: str,
    scope: str,
    item_id: str,
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
    )


@mcp.tool()
def memory_delete(tenant_id: str, scope: str, item_id: str, hard: bool = False) -> dict:
    """删除一条记忆。hard=false 软删（遗忘，记录仍可追溯）；true 物理清除。"""
    return _call(
        "delete",
        {"tenant_id": tenant_id, "scope": scope, "item_id": item_id, "hard": hard},
    )


@mcp.tool()
def memory_evolve(tenant_id: str, scope: str, mode: str = "extract") -> dict:
    """触发记忆演进。mode=extract / associate / consolidate / forget，返回后台 job_id。"""
    return _call("evolve", {"tenant_id": tenant_id, "scope": scope, "mode": mode})


def main() -> int:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http"):
        # MCP 尚无凭据通道（见模块 docstring）：DEV 模式下空凭据即 ROOT，绑非
        # loopback 等于把 ROOT 级记忆工具暴露给整个网络。与 HTTP surface 同一道
        # 闸（审计 P1-4：此前 MCP HTTP 启动没调 check_dev_binding）。
        if _SRV.authenticator.mode() is AuthMode.DEV:
            try:
                check_dev_binding(os.environ.get("MCP_HOST", "127.0.0.1"))
            except ValidationError as validation_error:
                logging.error("FATAL: %s", validation_error)
                return 1
        mcp.run(transport="streamable-http")  # host/port 已在 FastMCP(...) 设好
    else:
        mcp.run()  # stdio（默认）——Claude Desktop / Claude Code 直接挂载
    return 0


if __name__ == "__main__":
    sys.exit(main())
