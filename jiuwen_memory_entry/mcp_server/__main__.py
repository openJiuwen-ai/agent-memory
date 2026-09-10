# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP surface 入口——基于 FastMCP，把记忆动词暴露为 MCP 工具。

每个工具都是对 ``MemoryAPI`` 同名方法的薄封装（经 ``jiuwen_memory_entry/core/api_contract``
共享契约校验与调用，与 HTTP/CLI 同源），本适配器只做协议翻译、零业务逻辑。内核进程内
装配一次、跨工具调用持有状态。

认证：``JIUWEN_MEMORY_MCP_AUTH_MODE``（required | dev，默认 required，失闭）。dev 模式使用
固定 local/developer ROOT 测试身份，仅供本地功能测试且只允许绑定回环地址。凭据经
``transport_security`` 按传输归一：stdio 读 ``AGENT_MEMORY_API_KEY``；Streamable HTTP
逐请求读 ``Authorization`` header 与 socket peer（工具的 ``ctx: Context`` 参数由 FastMCP
注入，不进模型可见 Schema）。

启动::

    pip install ".[mcp]"
    scripts/run-mcp.sh [config.yml ...]                       # stdio（默认）
    MCP_TRANSPORT=http MCP_PORT=8138 scripts/run-mcp.sh       # Streamable HTTP
"""

import asyncio
import ipaddress
import logging
import os
import sys
import uuid
from importlib import import_module
from typing import Any

# 本文件不用 ``from __future__ import annotations``：FastMCP 依赖运行时注解对象识别
# Context 参数并把它从工具 Schema 中排除，字符串化注解会破坏该机制。

# 复用 jiuwen_memory_entry/core 共享件（契约 invoke_api / 认证中间件 / 内核装配），
# 与 CLI 相同的 flat-import；启动脚本通过 PYTHONPATH 保证优先级，这里 append 仅兜底。
_BOOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_BOOT)
for _p in (os.path.join(_BOOT, "core"), _REPO):
    if _p not in sys.path:
        sys.path.append(_p)

load_layer = import_module("config_loader").load_layer
_profiles_module = import_module("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_module("server").Server

_api = import_module("jiuwen_memory.api")
Surface = _api.Surface
AgentMemoryError = _api.AgentMemoryError
ValidationError = _api.ValidationError
build_dev_authenticator = _api.build_dev_authenticator
invoke_api = import_module("jiuwen_memory_entry.core.api_contract").invoke_api
authenticated = import_module("jiuwen_memory_entry.core.auth_middleware").authenticated
credentials_for_transport = import_module(
    "jiuwen_memory_entry.mcp_server.transport_security"
).credentials_for_transport

try:
    _fastmcp = import_module("mcp.server.fastmcp")
    FastMCP = _fastmcp.FastMCP
    Context = _fastmcp.Context
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        'MCP surface 需要 mcp SDK：pip install ".[mcp]"（或 pip install mcp）'
    ) from exc

logger = logging.getLogger("agent-memory.mcp")

_AUTH_MODE_ENV = "JIUWEN_MEMORY_MCP_AUTH_MODE"
_ALLOW_DEV_NON_LOOPBACK_ENV = "JIUWEN_MEMORY_MCP_ALLOW_DEV_NON_LOOPBACK"
_AUTH_MODES = frozenset({"required", "dev"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()

# --- 内核：进程内装配一次，跨工具调用共享 --- #
_SRV = Server.build(load_config([OFFLINE] + [load_layer(p) for p in sys.argv[1:]]))


def _build_authenticator():
    """required 失闭（未装配生产认证器时拒绝业务调用）；dev 固定测试身份。"""
    mode = os.environ.get(_AUTH_MODE_ENV, "required").strip().lower()
    if mode not in _AUTH_MODES:
        raise ValidationError(f"invalid {_AUTH_MODE_ENV}: {mode!r}")
    if mode == "dev":
        logger.warning(
            "development authentication is enabled; credentials are ignored "
            "and this mode must not be used in production"
        )
        return build_dev_authenticator()
    return None


_AUTHENTICATOR = _build_authenticator()

mcp = FastMCP(
    "agent-memory",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8138")),
)


def _invoke_blocking(verb: str, payload: dict, *, context: Any = None):
    """同步执行体：认证 → 共享契约调用；失败抛错，让 MCP 客户端看到原因。

    必须在**无事件循环的线程**里跑——同步 ``MemoryAPI`` 方法在 api 层内部用
    ``asyncio.run`` 桥接协程（S02「同步/异步桥接」），遇运行中的 loop 即抛
    "cannot be called from a running event loop"。
    """
    request_id = uuid.uuid4().hex
    if _AUTHENTICATOR is None:
        raise RuntimeError(
            "MCP authentication is not configured; "
            f"set {_AUTH_MODE_ENV}=dev for local testing"
        )
    try:
        credentials = credentials_for_transport(_TRANSPORT, context=context)
    except ValidationError as validation_error:
        raise RuntimeError(str(validation_error)) from validation_error
    try:
        with authenticated(
            _AUTHENTICATOR, credentials, surface=Surface.MCP, request_id=request_id
        ) as security:
            return invoke_api(_SRV.api, verb, payload, security)
    except AgentMemoryError as api_error:
        raise RuntimeError(f"{type(api_error).__name__}: {api_error}") from api_error


async def _invoke(verb: str, payload: dict, *, context: Any = None):
    """工具统一入口：执行体放工作线程，结果经 await 回到事件循环。

    FastMCP 在事件循环线程**裸调**工具函数（func_metadata 对同步 fn 不做
    to_thread），而同步 MemoryAPI 方法内部自带 ``asyncio.run`` 桥——两者相遇
    必炸。``asyncio.to_thread`` 把两套 loop 隔离开：工作线程无运行中循环，
    内部桥接照常工作。认证上下文的工作线程内 set/reset 同线程配对。
    """
    return await asyncio.to_thread(
        _invoke_blocking, verb, payload, context=context
    )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_binding(host: str) -> None:
    """dev 认证只允许绑定回环地址；放开须显式环境变量（容器内）。"""
    requirement = getattr(_AUTHENTICATOR, "requires_loopback_binding", None)
    requires_loopback = bool(requirement()) if callable(requirement) else False
    if not requires_loopback or _is_loopback_host(host):
        return
    if os.environ.get(_ALLOW_DEV_NON_LOOPBACK_ENV, "").strip().lower() in _TRUE_VALUES:
        logger.warning(
            "development authentication is listening on non-loopback host %s; "
            "the deployment boundary must prevent remote access",
            host,
        )
        return
    raise ValidationError(
        "development authentication may bind only to a loopback host; "
        f"set {_ALLOW_DEV_NON_LOOPBACK_ENV}=true only inside an isolated container"
    )


# --- 工具：记忆生命周期（与 MemoryAPI 同名方法对应；参数名与签名零漂移，经
#     api_contract.parse_request 严格校验。docstring 面向模型撰写——模型凭它
#     决定何时调用、如何填参。）--- #


@mcp.tool()
async def memory_add(content: str, scope: dict, tags: list[str] | None = None,
               ctx: Context = None) -> list[dict]:
    """写入一条记忆。

    content: 记忆内容（自然语言文本）。
    scope: 归属坐标 {"org","user","agent","session","space"}，五维均可给空字符串；
        记忆写入该坐标名下，检索时同坐标可见，如 {"org":"acme","user":"alice"}。
    tags: 可选标签列表。
    返回写入的记忆单元列表（含 id，供后续 get/update/delete 引用）。
    """
    return await _invoke(
        "add", {"content": content, "scope": scope, "tags": tags}, context=ctx
    )


@mcp.tool()
async def memory_search(query: str, context: dict, top_k: int = 10,
                  with_trajectory: bool = False, ctx: Context = None) -> dict:
    """按「语义 + 关键词」双路混合检索记忆。

    query: 查询文本。
    context: 检索上下文 {"scope": {...同 memory_add 的归属坐标...}, "extensions": {}}；
        其中 scope 决定在哪个范围内召回。
    top_k: 返回条数上限。
    with_trajectory: true 时附带检索轨迹（各通道召回与融合得分）。
    """
    return await _invoke(
        "search",
        {"query": query, "context": context, "top_k": top_k,
         "with_trajectory": with_trajectory},
        context=ctx,
    )


@mcp.tool()
async def memory_list(scope: dict, offset: int = 0, limit: int = 100,
                ctx: Context = None) -> dict:
    """列出目标 scope 下已建索引的记忆单元（分页）。

    返回 {"items": [...], "count": 分页前匹配总数}。
    """
    return await _invoke(
        "list", {"scope": scope, "offset": offset, "limit": limit}, context=ctx
    )


@mcp.tool()
async def memory_get(unit_id: str, scope: dict, ctx: Context = None) -> dict:
    """按 id 读取单条记忆单元。unit_id 来自 memory_add 的返回或 memory_list 的 items。"""
    return await _invoke("get", {"unit_id": unit_id, "scope": scope}, context=ctx)


@mcp.tool()
async def memory_update(unit_id: str, scope: dict, patch: dict,
                  ctx: Context = None) -> dict:
    """修正一条记忆。patch 仅非 null 字段生效，形如
    {"content": "修正后内容", "tags": ["标签"], "mode": "supersede"}。
    mode: "supersede"（默认，非破坏式——生成新 id 新版本、旧版保留血缘）或
    "overwrite"（原地覆写同 id）。注意 supersede 返回的 id 可能与传入的不同。
    """
    return await _invoke(
        "update", {"unit_id": unit_id, "scope": scope, "patch": patch}, context=ctx
    )


@mcp.tool()
async def memory_delete(selector: dict, ctx: Context = None) -> list[str]:
    """按选择器删除记忆。selector 必须给出 unit_ids / tags / before / filters 之一
    （scope 只是限定范围，单独给它不算选择条件），各条件取「与」，形如
    {"unit_ids": ["mu_..."], "scope": {...}, "tags": ["过期"],
     "before": "2026-01-01T00:00:00", "mode": "forget"}。
    mode: "forget"（默认，遗忘可恢复）/"archive"（归档）/"downweight"（降权）/
    "purge"（物理删除，不可恢复）。返回命中的记忆单元 id 列表。
    """
    return await _invoke("delete", {"selector": selector}, context=ctx)


@mcp.tool()
async def memory_evolve(scope: dict, mode: str = "extract", ctx: Context = None) -> str:
    """触发记忆演进（extract 抽取派生 / associate 建立关联 / consolidate 巩固升华 /
    forget 清理过期）。异步执行，返回后台任务 id（job_id），用 memory_job_status 查询进度。
    """
    return await _invoke("evolve", {"scope": scope, "mode": mode}, context=ctx)


@mcp.tool()
async def memory_batch_add(items: list[dict], scope: dict | None = None,
                     tags: list[str] | None = None, continue_on_error: bool = True,
                     ctx: Context = None) -> dict:
    """批量写入多条记忆（一次调用，结果按输入顺序逐项对齐）。

    items: 写入条目数组，每项形如 {"content": "记忆内容"}；content 必填，
        也可逐项给 scope/tags 覆盖批级缺省值。
    scope: 批级缺省归属坐标——item 里不给 scope 的条目沿用它，形如
        {"org":"acme","user":"alice"}。
    tags: 批级缺省标签。
    continue_on_error: true（默认）时单条失败不中断整批。
    返回 {"outcomes": [...]}，每项含 index（与输入顺序对齐）与 units（成功）
    或 error/error_type（该条失败原因）。
    """
    return await _invoke(
        "batch_add",
        {"items": items, "scope": scope, "tags": tags,
         "continue_on_error": continue_on_error},
        context=ctx,
    )


@mcp.tool()
async def memory_job_status(job_id: str, scope: dict | None = None,
                      ctx: Context = None) -> dict:
    """查询后台任务状态——memory_evolve 返回的 job_id 用它查进度。

    返回 {"id","channel","mode","scope","status","detail"}；
    status: pending / running / succeeded / failed / cancelled。
    """
    return await _invoke("job_status", {"job_id": job_id, "scope": scope}, context=ctx)


@mcp.tool()
async def memory_job_cancel(job_id: str, ctx: Context = None) -> None:
    """取消一个尚未完成的后台任务（幂等：对已结束的任务再取消不报错）。"""
    return await _invoke("job_cancel", {"job_id": job_id}, context=ctx)


@mcp.tool()
async def memory_inspect(unit_ids: list[str], scope: dict,
                   ctx: Context = None) -> list[dict]:
    """治理检视：按 id 批量读取记忆单元的完整信息（含已失效的历史版本）。"""
    return await _invoke("inspect", {"unit_ids": unit_ids, "scope": scope}, context=ctx)


@mcp.tool()
async def memory_trace(unit_id: str, scope: dict, ctx: Context = None) -> list[dict]:
    """血缘回溯：沿 provenance 追溯一条记忆的演进来源链——派生记忆（evolve 抽取/
    巩固的产出）回指它由哪些源记忆演进而来。传入派生单元的 id，
    返回 [该单元, 各来源单元...]；非派生单元只返回自身。
    """
    return await _invoke("trace", {"unit_id": unit_id, "scope": scope}, context=ctx)


# --- 工具：异步写入变体（api 层同步/异步双入口的 async 侧）---------------------- #


@mcp.tool()
async def memory_add_async(content: str, scope: dict, tags: list[str] | None = None,
                           ctx: Context = None) -> list[dict]:
    """异步写入一条记忆（语义同 memory_add，直通引擎协程，等待完成并返回结果）。

    content: 记忆内容。scope: 归属坐标 {"org","user",...}。tags: 可选标签。
    """
    return await _invoke(
        "add_async", {"content": content, "scope": scope, "tags": tags}, context=ctx
    )


@mcp.tool()
async def memory_batch_add_async(items: list[dict], scope: dict | None = None,
                                 tags: list[str] | None = None,
                                 continue_on_error: bool = True,
                                 ctx: Context = None) -> dict:
    """异步批量写入（语义同 memory_batch_add，直通引擎协程）。"""
    return await _invoke(
        "batch_add_async",
        {"items": items, "scope": scope, "tags": tags,
         "continue_on_error": continue_on_error},
        context=ctx,
    )


# --- 工具：写入预检与长耗时摄入 -------------------------------------------------- #


@mcp.tool()
async def memory_check_write(scope: dict, ctx: Context = None) -> None:
    """写入预检：校验当前身份对 scope 的 WRITE 权限，不落盘。
    用于长耗时任务入队前确认权限，避免无权限请求占用队列。
    """
    return await _invoke("check_write", {"scope": scope}, context=ctx)


@mcp.tool()
async def memory_submit_ingest(content: str, scope: dict, source: str,
                         payload_id: str, source_ref: str,
                         ctx: Context = None) -> dict:
    """提交长耗时摄入任务（文档/视频等多模态内容），返回任务信息。

    content: 原始内容文本。scope: 归属坐标。source: 模态（text/document/audio/video）。
    payload_id: 原文缓存标识。source_ref: 源资产引用（如 file:///...）。
    后台 add 会再鉴权一次；用 memory_job_status 查任务进度。
    """
    return await _invoke(
        "submit_ingest",
        {"content": content, "scope": scope, "source": source,
         "payload_id": payload_id, "source_ref": source_ref},
        context=ctx,
    )


# --- 工具：管理面（admin 策略，MANAGE_POLICY 鉴权）------------------------------- #


@mcp.tool()
async def memory_admin_get(key: str, ctx: Context = None) -> str:
    """读取一项运行时策略的当前值（管理面操作，需 MANAGE_POLICY 权限）。"""
    return await _invoke("admin_get", {"key": key}, context=ctx)


@mcp.tool()
async def memory_admin_set(key: str, value: str, ctx: Context = None) -> None:
    """调整一项运行时策略（启停索引、检索/演进开关等；未知键抛错）。
    管理面操作，落审计。
    """
    return await _invoke("admin_set", {"key": key, "value": value}, context=ctx)


@mcp.tool()
async def memory_admin_all(ctx: Context = None) -> dict:
    """列出全部运行时策略及当前值（管理面操作）。"""
    return await _invoke("admin_all", {}, context=ctx)


# --- 工具：治理面（审计查询与链校验）---------------------------------------------- #


@mcp.tool()
async def memory_audit(filters: dict, limit: int = 100, ctx: Context = None) -> list[dict]:
    """审计查询：按条件检索审计留痕（谁在何时对哪条记忆做了什么操作）。

    filters: 形如 {"action": "add", "actor_user": "developer",
    "occurred_after": "2026-01-01T00:00:00"}；支持 action/layer/decision/
    target_id/actor_*/target_*/occurred_after/occurred_before。limit: 返回条数上限。
    """
    return await _invoke("audit", {"filters": filters, "limit": limit}, context=ctx)


@mcp.tool()
async def memory_verify_audit(ctx: Context = None) -> dict:
    """审计链完整性验证：校验审计事件链是否被篡改。

    未装配审计完整性 provider 的部署返回 unsupported 状态（不报错）。
    """
    return await _invoke("verify_audit", {}, context=ctx)


# --- 工具：跨 scope 授权（SHARE/REVOKE_SHARE 鉴权）------------------------------- #


@mcp.tool()
async def memory_grant(grant: dict, ctx: Context = None) -> dict:
    """新增一条跨 scope 授权：A 把自己资源的某些动作开放给 B。

    grant: 形如 {"grantor": {"org":"local","user":"developer"},
    "grantee": {"org":"local","agent":"helper"}, "actions": ["read"]}。
    actions 取值: read/write/update/delete/share/revoke_share/manage_principal/
    manage_space/manage_policy/read_audit/verify_audit/administer_system。
    返回携带 grant_id 的授权对象（供 revoke 精确撤销）。
    """
    return await _invoke("grant", {"grant": grant}, context=ctx)


@mcp.tool()
async def memory_revoke(grant: dict, ctx: Context = None) -> None:
    """回收一条授权（幂等）。grant 形状同 memory_grant。"""
    return await _invoke("revoke", {"grant": grant}, context=ctx)


# --- 工具：Space 管理（管理面，MANAGE_SPACE 鉴权）-------------------------------- #


@mcp.tool()
async def memory_create_space(spec: dict, ctx: Context = None) -> dict:
    """创建 space（多租户隔离的逻辑空间）。

    spec: {"org":"local","space":"team-a","display_name":"Team A"}；
    可选 principal_path（"user_agent"/"agent_user"）、policy、metadata、owner。
    返回 SpaceInfo（含状态与策略）。
    """
    return await _invoke("create_space", {"spec": spec}, context=ctx)


@mcp.tool()
async def memory_get_space(org: str, space: str, ctx: Context = None) -> dict:
    """读取单个 space 的基础信息与策略。"""
    return await _invoke("get_space", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_list_spaces(org: str, ctx: Context = None) -> list[dict]:
    """列出 org 下当前身份可见的全部 spaces。"""
    return await _invoke("list_spaces", {"org": org}, context=ctx)


@mcp.tool()
async def memory_update_space(org: str, space: str, patch: dict,
                        ctx: Context = None) -> dict:
    """修改 space：patch 仅非 null 字段生效，形如
    {"display_name":"Alpha"} 或 {"status":"frozen"}。
    """
    return await _invoke(
        "update_space", {"org": org, "space": space, "patch": patch}, context=ctx
    )


@mcp.tool()
async def memory_archive_space(org: str, space: str, ctx: Context = None) -> dict:
    """归档 space：保留读取、导出与审计能力，停止新写入。"""
    return await _invoke("archive_space", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_delete_space(org: str, space: str, ctx: Context = None) -> dict:
    """删除 space（当前实现仅 purge：物理删除真源与可重建索引，不可恢复）。"""
    return await _invoke("delete_space", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_export_space(org: str, space: str, include_audit: bool = True,
                        ctx: Context = None) -> str:
    """提交 space 导出（含记忆与可选审计），返回 export id。"""
    return await _invoke(
        "export_space", {"org": org, "space": space, "include_audit": include_audit},
        context=ctx,
    )


@mcp.tool()
async def memory_space_usage(org: str, space: str, ctx: Context = None) -> dict:
    """查询 space 级用量（记忆数/消息数/索引数/存储字节/审计数）。"""
    return await _invoke("space_usage", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_get_space_policy(org: str, space: str, ctx: Context = None) -> dict:
    """读取 space 级策略（主体路径/隔离策略/保留期/配额等）。"""
    return await _invoke("get_space_policy", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_set_space_policy(org: str, space: str, policy: dict,
                            ctx: Context = None) -> dict:
    """替换 space 级策略。policy 形如
    {"require_space": true, "principal_path": "user_agent"}。
    """
    return await _invoke(
        "set_space_policy",
        {"org": org, "space": space, "policy": policy},
        context=ctx,
    )


@mcp.tool()
async def memory_list_space_members(org: str, space: str,
                              ctx: Context = None) -> list[dict]:
    """列出 space 成员及其两轴角色（content_role 内容轴 / governance_role 治理轴）。"""
    return await _invoke("list_space_members", {"org": org, "space": space}, context=ctx)


@mcp.tool()
async def memory_add_space_member(org: str, space: str, member: dict,
                            ctx: Context = None) -> None:
    """添加或更新 space 成员。member 形如
    {"scope": {"org":"local","user":"bob"}, "content_role": "contributor",
     "governance_role": "none"}；
    content_role: reader/contributor/editor 等；member.scope 的 user/agent
    至多一维非空。
    """
    return await _invoke(
        "add_space_member",
        {"org": org, "space": space, "member": member},
        context=ctx,
    )


@mcp.tool()
async def memory_remove_space_member(org: str, space: str, member: dict,
                               ctx: Context = None) -> None:
    """移除 space 成员。member 为要移除的主体坐标，
    如 {"org":"local","user":"bob"}。
    """
    return await _invoke(
        "remove_space_member", {"org": org, "space": space, "member": member},
        context=ctx,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    try:
        _check_binding(os.environ.get("MCP_HOST", "127.0.0.1"))
    except ValidationError as bind_error:
        logger.error("MCP server refused to start: %s", bind_error)
        return 2
    if _TRANSPORT in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")  # host/port 已在 FastMCP(...) 设好
    else:
        mcp.run()  # stdio（默认）——Claude Desktop / Claude Code 直接挂载
    return 0


if __name__ == "__main__":
    sys.exit(main())
