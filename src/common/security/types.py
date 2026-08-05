"""安全域公共值对象（F05「公共安全类型」）。

本模块只放**协议无关、跨能力共享**的值对象与身份传播原语：认证输入
（:class:`Credentials`）、认证产出（:class:`AuthContext`）、请求安全上下文
（:class:`RequestSecurityContext`）、密码学调用上下文（:class:`CryptoContext`）。

不放什么（F05 §公共安全类型）：

- 协议 payload 类型（HTTP/MCP 各自的请求体留在各 surface）；
- 存储业务对象（MemoryUnit / KV entry 等留在 ``common.type_def``）；
- 授权类型（``Action`` / ``ResourceDescriptor`` / ``Grant`` / ``Delegation``）——
  归 PR2 的 Authorization 域，本 PR 不提前定义。

与 :class:`~common.type_def.scope.Scope` 的职责分工没变：Scope 表达**资源归属**，
本模块表达**谁在操作、以什么凭据、在哪次请求里**。核心不变量（F05 §显式上下文
优于环境权限）：身份来自本模块的值对象，不来自 URI、请求体参数或未经校验的
HTTP header。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from common.type_def.scope import Scope

# ====================================================================== #
# 角色
# ====================================================================== #


class Role(str, Enum):
    """三级角色（F05 §认证不变量 2：role 只能来自服务端注册表或已验证 claim）。

    继承 ``str`` 使其可直接进 ``AuditEvent.detail``（``dict[str, str]``）
    与 JSON 序列化，无需额外转换。
    """

    USER = "user"  # 普通主体：只能在自己 scope 内操作
    ADMIN = "admin"  # 管理员：可管理本 org 内主体，不可跨 org
    ROOT = "root"  # 超级管理员：跨 org 全局


ROLE_RANK: dict[Role, int] = {Role.USER: 0, Role.ADMIN: 1, Role.ROOT: 2}
"""角色偏序，用于降级检测：签发方不得签出高于自身的角色。"""


# ====================================================================== #
# Surface 标识
# ====================================================================== #


class Surface(str, Enum):
    """请求接入形态。由适配层写入 :class:`RequestSecurityContext`，业务 payload 不可声明。"""

    HTTP = "http"
    MCP = "mcp"
    CLI = "cli"
    SDK = "sdk"
    INTERNAL = "internal"  # 进程内任务 / 后台 job


# ====================================================================== #
# 认证输入
# ====================================================================== #


@dataclass(frozen=True)
class Credentials:
    """一次认证所需的**原始凭据材料**（F05 §Credentials）。

    只保存协议无关且已规范化的数据。HTTP/MCP/CLI 的协议解析留在各自 surface，
    交到认证能力手上的必须已经是本结构。

    必须满足的约束（F05）：

    - 不包含目标资源 Scope——那是授权的输入，不是认证的；
    - 不接受 role / acting_user 等授权结果——认证只回答「你是谁」；
    - ``headers`` 的键在进入认证能力前已归一为小写（RFC 9110 §5.1 大小写不敏感）；
    - 敏感值不出现在 repr、错误消息或审计 detail 中——故 ``repr=False``。
    """

    api_key: str = field(default="", repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    peer_address: str = ""


# ====================================================================== #
# 认证产出
# ====================================================================== #


@dataclass(frozen=True)
class AuthContext:
    """认证完成后得到的**可信身份**（F05 §AuthContext）。

    不是客户端提交的数据结构：API Key、受信网关、OAuth 等不同认证路径最终都归一
    为本结构。任何 handler、业务参数或 LLM tool_call 都不得覆盖其中字段——故
    ``frozen=True``。

    ``actor`` **无默认值**，必须显式传入。给它默认值会让「忘了传 actor」静默产出
    空 ``Scope()``。

    **ROOT 由 ``role`` 表达，不由 actor 的形状表达**（F05 §授权不变量 1）。PR1 仍
    保留 ``actor == Scope()`` 的旧 platform-admin 兼容线供 ``PermissionManager``
    消费，该兼容线在 PR2 切换 Authorizer 时删除。

    ``delegation_id`` 只携带**已经服务端验证过的**委托标识；PR2 的 Authorizer 会拿它
    回真源复核。PR1 期间仍保留 ``acting_user`` 作为过渡接缝（旧 PermissionManager 的
    代操作判定依赖它），PR2 迁移到 DelegationStore 后删除。
    """

    actor: Scope  # 已认证的操作执行者
    role: Role = Role.USER  # 服务端角色注册表的产物，不来自请求
    credential_type: str = ""  # 本次使用的凭据类型（api_key / gateway / dev）
    credential_id: str = ""  # 凭据的不可逆标识（指纹），供撤销与审计；绝不是明文
    auth_method: str = ""  # 认证实现声明的方法标识（dev / trusted / api_key / ...）
    authenticated_at: datetime | None = None  # 服务端完成认证的时间
    expires_at: datetime | None = None  # 本次上下文的失效时间；None = 不随上下文过期
    delegation_id: str = ""  # 已验证的委托标识；PR2 由 DelegationStore 复核

    # -- PR1 过渡接缝（PR2 移除，见迁移计划 §5.2 第 4 项）------------------ #
    acting_user: str = ""  # 旧代操作字段：agent 代 user 操作时的委托目标

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """认证上下文是否已过期。``expires_at`` 为 None 表示不随上下文过期。"""
        if self.expires_at is None:
            return False
        reference = now if now is not None else datetime.now(tz=self.expires_at.tzinfo)
        return reference >= self.expires_at


# ====================================================================== #
# 请求安全上下文
# ====================================================================== #

_EMPTY_ATTRIBUTES: Mapping[str, str] = MappingProxyType({})


def _empty_attributes() -> Mapping[str, str]:
    """只读空映射的工厂。

    不能写成 ``field(default=_EMPTY_ATTRIBUTES)``：dataclass 以
    ``default.__class__.__hash__ is None`` 判定「可变默认值」，而 ``mappingproxy``
    在 Python 3.11 正是不可哈希的，import 阶段就会抛
    ``ValueError: mutable default ... use default_factory``（3.12 给它补了
    ``__hash__``，所以该阻断只在 3.11 暴露，而 3.11 是本项目的目标下限）。
    工厂每次都返回同一个只读常量，语义与 default 完全一致。
    """
    return _EMPTY_ATTRIBUTES


# RequestSecurityContext 的受控来源标记。只有受控构造入口（PR2 的
# ``new_request_context`` / ``internal_context``）构造时传 ``_TRUSTED``；直接
# ``RequestSecurityContext(...)`` 为 ``_UNTRUSTED``。PEP（PR2 起）校验
# ``_origin is _TRUSTED``，使「补齐 request_id/started_at 即可伪造上下文」不成立
# --字段形状完整不等于来自认证边界。sentinel 刻意不导出：增加绕过难度，且测试
# 用例直接构造不传 ``_origin`` 即被判为未受控。
_TRUSTED = object()
_UNTRUSTED = object()


@dataclass(frozen=True)
class RequestSecurityContext:
    """一次请求内供 API 安全边界使用的完整上下文（F05 §RequestSecurityContext）。

    PR2 起是 ``MemoryAPI`` 公开方法的**唯一显式安全输入**——业务 payload 中不再
    存在 ``identity`` / ``actor_*`` / ``role`` / ``acting_user`` 身份声明。PR1 先定义
    类型并由 surface 构造，MemoryAPI 签名的破坏性切换留给 PR2。

    ``attributes`` 只允许系统组件写入：它参与 PR2 的 ``AuthorizationEnvironment``，
    能从业务 payload 任意注入就等于把授权输入交给了调用方。构造时统一冻结成只读
    映射，让这条约束在类型层面成立而不只是文档约定。
    """

    auth: AuthContext
    request_id: str = ""  # 服务端生成或严格验证后的请求标识
    peer: str = ""  # 规范化后的连接来源
    surface: Surface = Surface.INTERNAL
    started_at: datetime | None = None
    attributes: Mapping[str, str] = field(default_factory=_empty_attributes)
    _origin: object = field(default=_UNTRUSTED, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.attributes, MappingProxyType):
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType({str(k): str(v) for k, v in self.attributes.items()}),
            )

    @property
    def actor(self) -> Scope:
        """已认证主体。授权的 actor 只能来自这里，不来自业务参数。"""
        return self.auth.actor


# ====================================================================== #
# 密码学调用上下文
# ====================================================================== #


@dataclass(frozen=True)
class CryptoContext:
    """一次加解密调用的安全上下文（F05 §CryptoContext）。

    由**存储适配器**构造：只有它同时掌握真实对象 id 与存储用途。业务请求不能直接
    控制 AAD——否则攻击者可以让两个不同对象共用同一 AAD，密文就能跨对象复制。

    ``format_version`` 进 AAD，使信封格式升级不会被降级重放。
    """

    scope: Scope
    purpose: str  # 存储用途（memory_unit / raw_message / fs_object / kv_value ...）
    object_id: str = ""  # 对象标识（KV key / FS ref）
    format_version: int = 1  # AAD 载荷格式版本
    metadata: Mapping[str, str] = field(default_factory=dict)


# ====================================================================== #
# 请求内身份传播（辅助通道）
# ====================================================================== #

_CURRENT: ContextVar[AuthContext | None] = ContextVar("auth_context", default=None)


def set_current(ctx: AuthContext) -> Token[AuthContext | None]:
    """在请求入口设置当前认证上下文；返回的 token 必须在请求结束时交给 reset。

    **定位（F05 §显式上下文优于环境权限）**：ContextVar 是日志关联与请求内辅助
    传播的通道，不是授权决策的唯一输入。PR1 仍由旧 ``LocalMemoryAPI`` →
    ``PermissionManager`` 经它透传（迁移计划 §4.3 明确列为临时接缝）；PR2 切到
    ``RequestSecurityContext`` 后，Authorizer 不再读取它。
    """
    return _CURRENT.set(ctx)


def reset_current(token: Token[AuthContext | None]) -> None:
    """请求结束时还原上下文。

    必须在 ``finally`` 中调用：``ThreadingHTTPServer`` 每请求一线程，线程可能
    被复用，漏 reset 会让下一个请求继承上一个请求的身份（最严重的一类越权）。
    """
    _CURRENT.reset(token)


def get_current() -> AuthContext | None:
    """取当前认证上下文；未认证返回 ``None``。

    刻意不返回默认 ``AuthContext``：那是 fail-open——中间件漏挂时请求会带着
    默认身份跑完。返回 ``None`` 迫使调用方显式处理（``handler.dispatch`` 的
    处理方式是抛 :class:`~common.errors.AuthenticationError`）。
    """
    return _CURRENT.get()
