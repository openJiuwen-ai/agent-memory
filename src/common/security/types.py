"""安全域公共值对象（F05「公共安全类型」）。

本模块只放**协议无关、跨能力共享**的值对象与身份传播原语：认证输入
（:class:`Credentials`）、认证产出（:class:`AuthContext`）、请求安全上下文
（:class:`RequestSecurityContext`）、授权输入（:class:`Action`、
:class:`ResourceDescriptor`、:class:`AuthorizationEnvironment`、:class:`Grant`、
:class:`Delegation`）、密码学调用上下文（:class:`CryptoContext`）。

不放什么（F05 §公共安全类型）：

- 协议 payload 类型（HTTP/MCP 各自的请求体留在各 surface）；
- 存储业务对象（MemoryUnit / KV entry 等留在 ``common.type_def``）；
- 授权**策略**与存储实现——类型在这里，判定归 ``security/authorization/``。

与 :class:`~common.type_def.scope.Scope` 的职责分工没变：Scope 表达**资源归属**，
本模块表达**谁在操作、以什么凭据、在哪次请求里**。核心不变量（F05 §显式上下文
优于环境权限）：身份来自本模块的值对象，不来自 URI、请求体参数或未经校验的
HTTP header。
"""

from __future__ import annotations

import hashlib
import hmac
import os
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

    **ROOT 由 ``role`` 表达，不由 actor 的形状表达**（F05 §授权不变量 1）。旧实现的
    ``actor == Scope()`` 隐式 platform-admin 兼容线已删除：空 actor 在
    ``StandardAuthorizer`` 是 **deny**，各认证实现的 ROOT 身份都带具名 actor。

    ``delegation_id`` 只携带**已经服务端验证过的**委托标识；Authorizer 拿它回
    ``DelegationStore`` 复核。这里刻意**没有** ``acting_user`` 之类的字段：一个 user 名
    只能表达「调用方声称在代谁操作」，表达不了「那个 user 真的授权过」（F05
    §从 header 直接产生 Delegation）。

    Round4 P1-4: 新增 ``credential_issuer`` 字段，用于 Registry 撤销路由。
    ``auth_method`` 保留协议/认证方法语义（"api_key" / "trusted" / "dev"），
    ``credential_issuer`` 携带具名实例名称（"primary_auth" / "partner_auth"）。
    """

    actor: Scope  # 已认证的操作执行者
    role: Role = Role.USER  # 服务端角色注册表的产物，不来自请求
    credential_type: str = ""  # 本次使用的凭据类型（api_key / gateway / dev）
    credential_id: str = ""  # 凭据的不可逆标识（指纹），供撤销与审计；绝不是明文
    auth_method: str = ""  # 认证实现声明的方法标识（dev / trusted / api_key / ...）
    credential_issuer: str = ""  # Round4: 凭据签发者标识（具名 Authenticator 实例名）
    authenticated_at: datetime | None = None  # 服务端完成认证的时间
    expires_at: datetime | None = None  # 本次上下文的失效时间；None = 不随上下文过期
    delegation_id: str = ""  # 已验证的委托标识；由 DelegationStore 复核

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


# RequestSecurityContext 的受控来源证明：绑定 auth 安全字段的 HMAC。
# 只有受控构造入口（new_request_context / internal_context）构造时计算并写入 _origin；
# PEP 校验 _origin == _bind_origin(security.auth)。dataclasses.replace 换 auth 但复制
# 旧 _origin -> HMAC 不匹配新 auth -> 拒。直接构造不传 _origin -> 空串 -> 拒。
# _ORIGIN_KEY 进程随机：进程外/跨进程无法伪造 HMAC。
_ORIGIN_KEY = os.urandom(32)


def _bind_origin(auth: AuthContext, context: "RequestSecurityContext | None" = None) -> str:
    """计算 auth 及完整安全上下文的来源绑定 token（HMAC-SHA256）。

    绑定 actor 五维 + role + credential 字段 + auth 字段，以及完整 RequestSecurityContext
    的安全字段（attributes、surface、peer 等）。replace 换掉其中任一字段后，旧 _origin
    与新上下文不匹配，PEP 拒。

    **威胁边界**：此 HMAC 方案仅防止跨进程伪造（_ORIGIN_KEY 是进程内随机密钥）。
    同进程代码可以调用 :func:`new_request_context` 构造任意 AuthContext 并获得有效签名，
    因此**无法防御恶意同进程组件**（业务插件、agent adapter、被注入的第三方代码）。
    若同进程代码不可信，需要进程隔离或 capability-based 设计。

    Round3: 绑定完整 RequestSecurityContext 安全字段，防止通过 replace(attributes=...)
    注入服务端安全 attributes 提权。

    Round4: 使用 NUL 分隔符明确边界，避免 attributes 序列化结构碰撞。
    Round4 P1-4: 绑定 credential_issuer 字段。

    Round5: 在每对 k-v 之后也添加额外 NUL 分隔符，防止通过 value 中嵌入 NUL 字符绕过。

    Round7 P1-1: 改用 Canonical JSON 序列化，彻底消除手写分隔符的结构歧义。
    JSON 的结构化编码天然防止 {"a": "b\\0\\0c\\0d"} 和 {"a": "b", "c": "d"} 碰撞。
    """
    import json

    # 构造结构化签名材料
    payload = {
        "auth": {
            "actor": {
                "org": auth.actor.org,
                "space": auth.actor.space,
                "user": auth.actor.user,
                "agent": auth.actor.agent,
                "session": auth.actor.session,
            },
            "role": auth.role.value,
            "credential_type": auth.credential_type,
            "credential_id": auth.credential_id,
            "auth_method": auth.auth_method,
            "credential_issuer": auth.credential_issuer,
            "authenticated_at": auth.authenticated_at.isoformat() if auth.authenticated_at else "",
            "delegation_id": auth.delegation_id,
        }
    }

    # Round3: 绑定完整安全上下文字段（若提供）
    if context is not None:
        payload["context"] = {
            "surface": context.surface.value if context.surface else "",
            "peer": context.peer or "",
            "request_id": context.request_id,
            "started_at": context.started_at.isoformat() if context.started_at else "",
            "attributes": dict(sorted(context.attributes.items())),
        }

    # Canonical JSON: sort_keys 保证顺序，separators 消除空格，ensure_ascii=False 保留 Unicode
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    return hmac.new(_ORIGIN_KEY, material.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class RequestSecurityContext:
    """一次请求内供 API 安全边界使用的完整上下文（F05 §RequestSecurityContext）。

    ``MemoryAPI`` 公开方法的**唯一显式安全输入**——业务 payload 中不存在
    ``identity`` / ``actor_*`` / ``role`` / ``acting_user`` 身份声明。由
    ``bootstrap.core.auth_middleware.authenticated`` 在认证后构造，经参数一路传到
    PEP；不经 ContextVar（那条通道已降级为日志辅助，见 :func:`set_current`）。

    ``attributes`` 只允许系统组件写入：它参与 ``AuthorizationEnvironment``，能从业务
    payload 任意注入就等于把授权输入交给了调用方。构造时统一冻结成只读映射，让这条
    约束在类型层面成立而不只是文档约定。
    """

    auth: AuthContext
    request_id: str = ""  # 服务端生成或严格验证后的请求标识
    peer: str = ""  # 规范化后的连接来源
    surface: Surface = Surface.INTERNAL
    started_at: datetime | None = None
    attributes: Mapping[str, str] = field(default_factory=_empty_attributes)
    _origin: str = field(default="", repr=False, compare=False)

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
# 授权：动作
# ====================================================================== #


class Action(str, Enum):
    """封闭的安全动作集合（F05 §Action）。

    **封闭**是这个类型的全部意义：授权策略按成员穷举，新增成员默认不属于任何角色、
    Grant 或 Delegation，必须显式配置后才能放行（F05 §授权不变量 5）。用开放字符串
    表达动作会让「拼错的动作名」和「未配置的新动作」在策略里长得一样，而前者应该
    是错误、后者应该是拒绝。

    与 ``Authenticator.mode()`` 的开放字符串刚好相反：模式影响的是**如何认证**，
    第三方实现要能自带；动作影响的是**准许什么**，第三方不能自行扩张。
    """

    # -- 数据动作 -------------------------------------------------------- #
    READ = "read"  # 读取/检索
    WRITE = "write"  # 写入新记忆
    UPDATE = "update"  # 修正已有记忆
    DELETE = "delete"  # 遗忘/降权/归档

    # -- 分享动作 -------------------------------------------------------- #
    SHARE = "share"  # 再授权给其他 scope
    REVOKE_SHARE = "revoke_share"  # 回收已授出的分享

    # -- 管理动作 -------------------------------------------------------- #
    MANAGE_PRINCIPAL = "manage_principal"  # 主体注册表：签发/撤销/改角色
    MANAGE_SPACE = "manage_space"  # space 生命周期与策略
    MANAGE_POLICY = "manage_policy"  # 治理策略

    # -- 审计动作 -------------------------------------------------------- #
    READ_AUDIT = "read_audit"  # 查询审计事件
    VERIFY_AUDIT = "verify_audit"  # 校验审计链完整性

    # -- 系统动作 -------------------------------------------------------- #
    ADMINISTER_SYSTEM = "administer_system"  # 跨 org 的系统级操作


MANAGEMENT_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.MANAGE_PRINCIPAL,
        Action.MANAGE_SPACE,
        Action.MANAGE_POLICY,
        Action.READ_AUDIT,
        Action.VERIFY_AUDIT,
        Action.ADMINISTER_SYSTEM,
    }
)
"""管理面动作：需要角色闸门，且默认不可委托（F05 §授权不变量 4）。"""

DELEGATABLE_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.WRITE, Action.UPDATE, Action.DELETE}
)
"""允许出现在 Delegation allowlist 中的动作。

刻意用**白名单**而非「管理动作取反」：取反写法下，新增的动作会自动变得可委托，
而 F05 §授权不变量 5 要求新 Action 默认拒绝。``SHARE`` / ``REVOKE_SHARE`` 不在列内
——让被委托方能再授权，等于让委托关系自我复制，撤销就追不上了。
"""


# ====================================================================== #
# 授权：拒绝原因
# ====================================================================== #


class DenyReason(str, Enum):
    """稳定的拒绝原因码（F05 §可观测性：授权决策记录稳定 reason code）。

    稳定指：值是审计与告警的匹配依据，改名等于破坏下游。文案可以变，值不可以。

    刻意**不**细分到「资源不存在」与「无权访问」——那是资源枚举侧信道。二者都归
    ``NOT_COVERED``。
    """

    EXPIRED_CONTEXT = "expired_context"  # AuthContext 已过期
    CONTEXT_MISMATCH = "context_mismatch"  # actor 与请求安全上下文不一致
    ROLE_REQUIRED = "role_required"  # 未通过角色闸门
    CROSS_ORG = "cross_org"  # 跨 org 硬边界
    CROSS_SPACE = "cross_space"  # 跨 space 硬边界
    NOT_COVERED = "not_covered"  # actor 不覆盖 target，且无有效 Grant/Delegation
    DELEGATION_INVALID = "delegation_invalid"  # 委托不存在/已撤销/已过期/绑定不符
    DELEGATION_ACTION = "delegation_action"  # 动作不在委托 allowlist 内
    GRANT_INVALID = "grant_invalid"  # 授权不存在/已撤销/已过期
    DEFAULT_DENY = "default_deny"  # 兜底：没有任何规则放行


# ====================================================================== #
# 授权：资源描述
# ====================================================================== #


@dataclass(frozen=True)
class ResourceDescriptor:
    """一次授权判定的**目标资源**（F05 §ResourceDescriptor）。

    由 ``MemoryAPI``（唯一业务 PEP）构造。核心约束：**已有资源的安全 metadata 必须
    来自真源**——请求只能提交筛选意图，不能声明决定授权结果的资源属性。若允许请求
    自述「这条记忆属于我」，授权就退化成了信任调用方。

    ``scope`` 是资源的真实归属，不是请求里写的那个；对已存在的 unit，它必须由
    API/Engine 从存储真源读出。
    """

    action: Action
    resource_type: str  # write_input / query / memory_unit / admin / job ...
    scope: Scope  # 资源真实归属（真源）
    resource_id: str = ""  # 已存在资源的 id；新建操作为空
    attributes: Mapping[str, str] = field(default_factory=_empty_attributes)

    def __post_init__(self) -> None:
        if not isinstance(self.attributes, MappingProxyType):
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType({str(k): str(v) for k, v in self.attributes.items()}),
            )


# ====================================================================== #
# 授权：环境
# ====================================================================== #


@dataclass(frozen=True)
class AuthorizationEnvironment:
    """授权判定的**环境输入**（F05 §Authorization）。

    只包含服务端产生的时间、部署策略和请求安全属性。**不包含**调用方声明的 role
    或资源安全 metadata——那两样一旦可由请求提供，授权就成了自证。

    ``now`` 显式传入而非在 Authorizer 内部取：过期判定要可测试，也要保证同一次判定
    里所有时效检查用同一个时刻（Grant 与 Delegation 分别取一次 ``now`` 会出现一个
    刚好过期、另一个还没过期的裂缝）。
    """

    now: datetime
    surface: Surface = Surface.INTERNAL
    request_id: str = ""
    peer: str = ""
    attributes: Mapping[str, str] = field(default_factory=_empty_attributes)

    def __post_init__(self) -> None:
        if not isinstance(self.attributes, MappingProxyType):
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType({str(k): str(v) for k, v in self.attributes.items()}),
            )

    @classmethod
    def from_request(
        cls, security: RequestSecurityContext, *, now: datetime
    ) -> AuthorizationEnvironment:
        """从请求安全上下文派生环境。

        只取 surface/request_id/peer/attributes 四项——它们都由服务端组件写入。
        ``auth`` 不进环境：身份是 Authorizer 的独立入参，混进环境会让「谁在操作」
        和「在什么条件下操作」两件事在策略里纠缠。
        """
        return cls(
            now=now,
            surface=security.surface,
            request_id=security.request_id,
            peer=security.peer,
            attributes=security.attributes,
        )


# ====================================================================== #
# 授权：长期授权与代操作
# ====================================================================== #


@dataclass(frozen=True)
class Grant:
    """主体之间的**显式长期授权**（F05 §Grant）。

    与 :class:`Delegation` 的分工：Grant 是「A 把自己资源的某些动作开放给 B」，
    是资源侧的长期开放；Delegation 是「user 授权 agent 代表自己行事」，是身份侧的
    有限期代理。两者的撤销语义、时效要求和可委托动作都不同，合成一个类型会让
    「撤销了代理，分享还在」这类正确行为难以表达。

    ``grant_id`` 是服务端生成的审计标识：撤销、审计与告警都按它定位。
    """

    grant_id: str
    grantor: Scope
    grantee: Scope
    actions: frozenset[Action]
    expires_at: datetime | None = None  # None = 长期有效
    revoked: bool = False

    def is_active(self, *, now: datetime) -> bool:
        """当前是否有效（未撤销且未过期）。"""
        if self.revoked:
            return False
        return self.expires_at is None or now < self.expires_at


@dataclass(frozen=True)
class Delegation:
    """user 对 agent/service 的**可撤销、有限期代操作授权**（F05 §Delegation）。

    存在的理由是 F05 §从 header 直接产生 Delegation 那条拒绝：网关 header 最多证明
    「网关声称这是某个 user」，不能证明「该 user 真的授权了这个 agent」。委托必须是
    服务端事实，由 ``delegation_id`` 回真源复核。

    ``expires_at`` **无默认值**：代操作授权必须有限期。给它一个「None = 永久」的默认，
    等于让忘记设置过期时间静默产出一个永久代理。

    ``bound_credential_id`` 把委托绑到具体凭据：换一把 key 的同一个 agent 用不了这条
    委托，凭据泄露的爆炸半径就收敛在单把 key 上。
    """

    delegation_id: str
    delegator: Scope  # 委托方（user）
    delegate: Scope  # 被委托方（agent / service）
    actions: frozenset[Action]  # 动作 allowlist
    expires_at: datetime
    not_before: datetime | None = None
    revoked: bool = False
    allowed_spaces: frozenset[str] = frozenset()  # 空 = 不额外限制 space
    bound_credential_id: str = ""  # 绑定凭据；空 = 不绑定
    bound_session: str = ""  # 绑定会话；空 = 不绑定

    def is_active(self, *, now: datetime) -> bool:
        """当前是否在有效期内且未撤销。"""
        if self.revoked:
            return False
        if self.not_before is not None and now < self.not_before:
            return False
        return now < self.expires_at

    def permits(self, action: Action) -> bool:
        """动作是否在 allowlist 内**且**本身可委托。

        两个条件缺一不可：allowlist 由数据决定、``DELEGATABLE_ACTIONS`` 由策略决定。
        只查 allowlist 会让一条写坏或被篡改的委托记录直接拿到管理动作。
        """
        return action in DELEGATABLE_ACTIONS and action in self.actions


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

    **定位（F05 §显式上下文优于环境权限）**：ContextVar 已降级为日志/trace 的辅助
    传播通道（迁移计划 §5.2 第 10 项）。安全语义全部走显式参数——
    ``auth_middleware.authenticated`` 产出 :class:`RequestSecurityContext`，surface
    显式传给 ``dispatch``、``dispatch`` 传给 ``MemoryAPI``、``MemoryAPI`` 传给
    ``Authorizer``。**没有任何授权路径读这里**：新增消费方前请先确认，你要的不是
    「把 security 参数一路传下去」。
    """
    return _CURRENT.set(ctx)


def reset_current(token: Token[AuthContext | None]) -> None:
    """请求结束时还原上下文。

    必须在 ``finally`` 中调用：``ThreadingHTTPServer`` 每请求一线程，线程可能
    被复用，漏 reset 会让下一个请求继承上一个请求的身份。授权已不读它，故这不再是
    越权路径，但漏 reset 会让日志把两个请求归到同一主体名下。
    """
    _CURRENT.reset(token)


def get_current() -> AuthContext | None:
    """取当前认证上下文；未认证返回 ``None``。**只用于日志/trace**。

    刻意不返回默认 ``AuthContext``：那是 fail-open，会让「中间件漏挂」在读取侧
    看起来像「有个匿名身份」。返回 ``None`` 迫使调用方显式处理缺失。
    """
    return _CURRENT.get()
