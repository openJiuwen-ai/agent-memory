# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""安全域：认证、密码学、资源保护与请求安全上下文（F05 Common Security）。

本包是安全能力的**唯一归属地**。消费方（Bootstrap/Surface、MemoryAPI、Storage
适配器、Audit）只 import 本包的契约与值对象，不反向被 import。

各能力子包按 F05 目录组织，``authorization/`` 与 ``audit_integrity/`` 的契约已随 PR2、
PR3 的接口分支固定；本实装 PR 补 authentication / cryptography / protection 三块的
实现，``authorization_impl/`` 只含 PR1 的 ``allow_all`` 占位（Runtime 必填
``authorizer`` 字段得装一个有值可填的占位，``is_test_only()`` 为真、不做判定；
做判定的 ``StandardAuthorizer`` 随 PR2 合入），``audit_integrity/`` 保持只有契约、
没有生产实现。本模块只再导出跨能力共享的公共类型、
``RequestSecurityContext`` 的受控构造入口与 Runtime--各能力的契约从其子包取
（``common.security.authentication`` 等），避免顶层 ``__init__`` 变成什么都有的入口
而在装配前意外触发全部 import。

旧加密模块（``SecurityProvider`` 系与 ``KeySource``，服务于存储加密装配）已随本
实装 PR 删除：存储加密改用 :mod:`common.security.cryptography` 的
``CryptographyProvider`` / ``KeyProvider``，配置顶层段 ``security`` 由
:class:`~common.security.runtime.SecurityRuntimeProducer` 接管。
"""

from .request_context import internal_context, new_request_context
from .runtime import SecurityRuntime, SecurityRuntimeProducer
from .types import (
    AuthContext,
    Credentials,
    CryptoContext,
    RequestSecurityContext,
    Role,
    Surface,
)

__all__ = [
    "AuthContext",
    "Credentials",
    "CryptoContext",
    "RequestSecurityContext",
    "Role",
    "SecurityRuntime",
    "SecurityRuntimeProducer",
    "Surface",
    "internal_context",
    "new_request_context",
]
