# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TRUSTED 认证：信任上游网关已完成认证（F05 §Authentication）。

网关注入身份声明 header，框架据此构造 actor。**关键设计：role 不从 header 读**
——header 说「你是谁」，框架自己查注册表得「你能干什么」。这样即使网关被攻破
或误配，攻击者也无法通过伪造 ``X-Role: root`` 提权。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    PrincipalKeyStore,
    fingerprint,
)
from jiuwen_memory.common.security.types import AuthContext, Credentials, reveal_secret
from jiuwen_memory.common.type_def.scope import Scope

_LOG = logging.getLogger(__name__)

# header 名硬编码，不做成配置项：没有第二个网关约定的时候，可配置只是多一处
# 误配可能（配错了就静默认证失败）。gateway_key 是配置项，因为它是部署相关
# 的秘密，必须能从环境变量注入。
#
# 键为小写：HTTP header 名大小写不敏感（RFC 9110 §5.1），
# ``credentials_from_headers`` 已把所有键归一为小写。
_H_ORG = "x-org-id"
_H_TYPE = "x-principal-type"
_H_ID = "x-principal-id"
_PRINCIPAL_TYPES = frozenset({"user", "agent"})

_METHOD = "trusted"  # 开放字符串而非封闭枚举（F05 拒绝以模式名驱动核心分支）
_CREDENTIAL = "gateway"


def _credential_id(gateway_key: str, org: str, principal_type: str, principal_id: str) -> str:
    """TRUSTED 凭据的不可逆标识：网关凭据指纹 + 主体三元组的 sha256。

    F05 §认证不变量 5 要求每条凭据有 credential id，供撤销、审计关联与 Delegation
    的 ``bound_credential_id`` 绑定。TRUSTED 的「凭据」是「网关以 ``gateway_key``
    担保的主体身份」--故标识必须含 ``gateway_key`` 指纹：网关凭据轮换后，同一主体
    得到不同 credential_id，旧凭据绑定的委托不能迁移到新凭据。单算主体三元组的
    指纹做不到这点，且 user id 常低熵可枚举。
    """
    material = f"{fingerprint(gateway_key)}:{org}:{principal_type}:{principal_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_FAILED = "authentication failed"


class TrustedAuthenticator(Authenticator):
    """读网关注入的身份声明，角色查本地注册表。"""

    def __init__(self, key_store: PrincipalKeyStore, gateway_key: str = "") -> None:
        self._key_store = key_store
        self._gateway_key = gateway_key

    def authenticate(self, credentials: Credentials) -> AuthContext:
        headers = credentials.headers
        org = str(headers.get(_H_ORG, "")).strip()
        principal_type = str(headers.get(_H_TYPE, "")).strip().lower()
        principal_id = str(headers.get(_H_ID, "")).strip()

        if not org or principal_type not in _PRINCIPAL_TYPES or not principal_id:
            raise AuthenticationError(_FAILED)

        # 网关到框架这一跳的共享密钥（可选）：配了就必须对上，防止绕过网关直连。
        # encode 成 bytes 再比：compare_digest 的 str 版对非 ASCII 输入抛 TypeError。
        if self._gateway_key and not hmac.compare_digest(
            self._gateway_key.encode("utf-8"), credentials.api_key.encode("utf-8")
        ):
            raise AuthenticationError(_FAILED)

        # keyword 构造：F03 将给 Scope 加 space 字段，位置参数会错位。
        actor = Scope(org=org, **{principal_type: principal_id})

        role = self._key_store.get_role(actor)
        if role is None:
            # 未注册主体一律拒绝，不默认给 USER 放行——fail-closed。
            raise AuthenticationError(_FAILED)

        return AuthContext(
            actor=actor,
            role=role,
            credential_type=_CREDENTIAL,
            credential_id=_credential_id(self._gateway_key, org, principal_type, principal_id),
            auth_method=_METHOD,
            authenticated_at=datetime.now(timezone.utc),
        )

    def mode(self) -> str:
        return _METHOD

    def requires_loopback_binding(self) -> bool:
        return False

    def requires_concurrency_guard(self) -> bool:
        return False

    def health(self) -> None:
        self._key_store.health()


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@AuthProducer.register("trusted")
def _build(config):
    gateway_key = reveal_secret(config.get("gateway_key", "")).strip()
    if not gateway_key:
        # 未配 gateway_key 时，全部身份 header（X-Org-Id / X-Principal-* 等）可被
        # 任意能连到本端口的调用方伪造。默认拒绝启动；确需仅靠网络隔离时，必须
        # 显式 opt-in，让「没有网关密钥」成为一个可见的部署决定而非默认状态。
        if not _truthy(config.get("allow_no_gateway_key", False)):
            raise ValidationError(
                "trusted 模式必须配置 gateway_key：未配置时身份 header 可被任意调用方"
                "伪造。若确需仅靠网络隔离（受信反代/mTLS 已到位），显式设"
                " allow_no_gateway_key=true。"
            )
        _LOG.warning(
            "trusted 模式未配 gateway_key（allow_no_gateway_key=true）：信任全部"
            "身份 header，仅可用于网络已隔离的部署。"
        )
    key_store = KeyStoreProducer.dep(config, "key_store", default="memory")
    return TrustedAuthenticator(key_store=key_store, gateway_key=gateway_key)
