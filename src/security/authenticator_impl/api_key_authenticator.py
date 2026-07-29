"""API_KEY 认证：框架自校验 API Key（security.md §2.2.3）。

两步：先常时间比对配置声明的 Root API Key，未命中再查主体注册表。
Root Key **不入注册表**（§2.3.1）——它是部署级凭据，不属于任何 org。
"""

from __future__ import annotations

import hmac
import logging

from common.errors import AuthenticationError
from common.type_def.auth import AuthContext, Role
from common.type_def.scope import Scope
from security.authenticator import Authenticator, AuthProducer
from security.key_store import KeyStoreProducer, PrincipalKeyStore
from security.types import AuthMode, Credentials

_LOG = logging.getLogger(__name__)

_FAILED = "authentication failed"


class ApiKeyAuthenticator(Authenticator):
    """Root Key 常时间比对 + 主体注册表查询。"""

    def __init__(self, key_store: PrincipalKeyStore, root_api_key: str = "") -> None:
        self._key_store = key_store
        self._root_key = root_api_key

    def authenticate(self, credentials: Credentials) -> AuthContext:
        api_key = credentials.api_key
        if not api_key:
            raise AuthenticationError(_FAILED)

        # Step 1: Root API Key。
        # encode 成 bytes 再比：compare_digest 的 str 版要求两边都是 ASCII-only，
        # 攻击者提交的非 ASCII key 会让它抛 TypeError（→ 500 而非 401），
        # 且泄露「你提交了非 ASCII」。str.encode 对任何 str 都成功，且
        # compare_digest 对长度不等的输入仍不早退。
        if self._root_key and hmac.compare_digest(
            self._root_key.encode("utf-8"), api_key.encode("utf-8")
        ):
            return AuthContext(actor=Scope(), role=Role.ROOT)

        # Step 2: 主体注册表（内部已做常时间比对与 dummy pad）。
        identity = self._key_store.resolve(api_key)
        if identity is None:
            raise AuthenticationError(_FAILED)
        return identity

    def mode(self) -> AuthMode:
        return AuthMode.API_KEY

    def health(self) -> None:
        self._key_store.health()


@AuthProducer.register("api_key")
def _build(config):
    root_key = str(config.get("root_api_key", "") or "").strip()
    if not root_key:
        # 引导问题（§3.5）：没有 root key 就没人能签发第一把主体 key。
        # 第一期只警告不阻断——root key 已轮换掉、只留主体 key 的部署是合法的。
        _LOG.warning(
            "api_key 认证模式未配置 root_api_key：无法签发首把主体 key。"
            "若这是有意的（root key 已轮换），可忽略本警告。"
        )
    key_store = KeyStoreProducer.dep(config, "key_store", default="memory")
    return ApiKeyAuthenticator(key_store=key_store, root_api_key=root_key)
