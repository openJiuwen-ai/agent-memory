"""AuthContext — 认证上下文（横切结构，security.md §7.1）。

认证层（``src/security``）校验凭据后产出本结构，经 ContextVar 在请求内传播，
供 PEP（``LocalMemoryAPI._authorize``）、特权闸门与审计消费。

与 :class:`~common.type_def.scope.Scope` 职责不同：Scope 表达**资源归属**，
本结构表达**谁在操作、以什么身份、凭什么凭据**。鉴权通过后只把 target scope
下沉到 Engine/Store，认证元数据不污染存储接口。

核心不变量（security.md §1.1）：身份来自本结构，不来自 URI、请求体参数或
未经校验的 HTTP header。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum

from .scope import Scope


class Role(str, Enum):
    """三级角色（security.md §3.1）。

    继承 ``str`` 使其可直接进 ``AuditEvent.detail``（``dict[str, str]``）
    与 JSON 序列化，无需额外转换。
    """

    USER = "user"  # 普通主体：只能在自己 scope 内操作
    ADMIN = "admin"  # 管理员：可管理本 org 内主体，不可跨 org
    ROOT = "root"  # 超级管理员：跨 org 全局


ROLE_RANK: dict[Role, int] = {Role.USER: 0, Role.ADMIN: 1, Role.ROOT: 2}
"""角色偏序，用于降级检测（security.md §3.1）：签发方不得签出高于自身的角色。"""


@dataclass(frozen=True)
class AuthContext:
    """认证层完成凭据校验后产出的**可信**请求级安全上下文。

    不是客户端提交的数据结构：API Key、受信网关等不同认证路径最终都归一为
    本结构。任何 handler、业务参数或 LLM tool_call 都不得覆盖其中字段——
    故 ``frozen=True``。

    ``actor`` **无默认值**，必须显式传入。给它默认值等于让「忘了传 actor」
    静默产出一个空 ``Scope()`` 身份，而空 scope 在 ACL 里是「覆盖一切」的通配
    形态（见 ``_owner_scope_covers``）——最糟糕的 fail-open。

    注意 ROOT **不由 actor 的形状决定**：``role`` 才是唯一依据。带
    ``AuthContext`` 调用时 ``SQLitePermissionManager.check`` 会显式拒绝空
    ``actor``；``actor == Scope()`` 的 platform-admin 规则只在**没有**认证
    上下文时保留（后台 job / 单测 / ``build_kernel`` 直连）。
    """

    actor: Scope  # 已认证的操作执行者；不得为空 Scope()
    acting_user: str = ""  # 当前操作对应的 user；agent 代操作时为委托目标
    role: Role = Role.USER  # 服务端角色注册表的产物，不来自请求
    from_oauth: bool = False  # 区分 OAuth 与 API Key 路径（第二期消费）
    authorizing_key_fp: str = ""  # 签发本次凭据的 key 指纹，供轮换级联失效与追责


_CURRENT: ContextVar[AuthContext | None] = ContextVar("auth_context", default=None)


def set_current(ctx: AuthContext) -> Token[AuthContext | None]:
    """在请求入口设置当前认证上下文；返回的 token 必须在请求结束时交给 reset。"""
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
