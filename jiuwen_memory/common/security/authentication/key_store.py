"""主体 API Key 凭据存储契约（security.md §2.3）。

只负责「key ↔ 主体身份」的映射：签发、解析、撤销、查角色。不做认证分流
（那是 :class:`~common.security.authentication.base.Authenticator` 的事），也不管 Root API
Key——它不入注册表，由 api_key authenticator 单独 ``compare_digest`` 比对
（§2.3.1）。
"""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def.scope import Scope

_PREFIX_LEN = 8  # 前缀索引长度（§2.3.1）
_KEY_BYTES = 32  # 256 bit


class KeyStoreProducer(Factory):
    """PrincipalKeyStore 的注册式工厂（与契约同处接口层）。

    各实现在 ``authentication_impl`` 下以 ``@KeyStoreProducer.register("<后端>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "key_store"


def fingerprint(api_key: str) -> str:
    """key 的 sha256 十六进制指纹。

    三个用途：(1) 注册表的确定性查找键——Argon2 每次 salt 不同，哈希值不能作键；
    (2) 撤销的定位键；(3) 未来 OAuth token 的绑定锚（§6.5）。

    **必须在哈希之前用明文算**——密码哈希不可逆，事后无法补算。

    指纹不可逆但**可枚举**（若 key 空间小可暴力），故 key 生成必须高熵，
    见 :func:`generate_api_key`。指纹本身进审计日志是安全的。
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def key_prefix(api_key: str) -> str:
    """前缀索引键：定位候选记录，避免 resolve 全表扫描（§2.3.1）。

    43 字符 URL-safe base64 的前 8 字符约 48 bit，候选列表基本恒为 1。
    前缀索引本身是**非常时间**的 dict 查找（已知缝隙，§2.3.2），由 resolve
    未命中时的 dummy verify 补偿。
    """
    return api_key[:_PREFIX_LEN]


def generate_api_key() -> str:
    """生成一把高熵 API Key：43 字符 URL-safe base64，256 bit 熵。

    必须用 ``secrets`` 而非 ``random``——后者是可预测的 Mersenne Twister。
    """
    return secrets.token_urlsafe(_KEY_BYTES)


class PrincipalKeyStore(ABC):
    """主体 API Key 注册表：签发、解析、撤销。"""

    @abstractmethod
    def issue(self, actor: Scope, role: Role) -> str:
        """为 ``actor`` 签发一把 API Key，返回**一次性明文**。

        明文只在此刻返回一次，服务端随后只保存验证材料（密码哈希 + sha256
        指纹）。

        ``role`` 不得为 :attr:`~common.security.types.Role.ROOT`——ROOT 只能来自
        配置声明的 Root API Key（§3.2「明确禁止」自签发 ROOT），传 ROOT 抛
        :class:`~common.errors.PermissionDeniedError`。

        ``actor`` 必须且只能指定 ``user`` 或 ``agent`` 之一（§4.1），否则抛
        :class:`~common.errors.ValidationError`。
        """

    @abstractmethod
    def resolve(self, api_key: str) -> AuthContext | None:
        """按明文 key 反查主体身份；未命中返回 ``None``。

        **本方法允许返回 None**，与 :meth:`~common.security.authentication.base.
        Authenticator.authenticate` 不同：它是「查表未命中」的事实陈述，由调用方翻译成
        ``AuthenticationError``。这不构成 fail-open——调用方拿到 None 唯一能做的
        就是拒绝。

        实现必须满足 §2.3.2：前缀索引定位候选、常时间比对、**未命中时补一次
        dummy verify** 把耗时 pad 到与命中路径同量级，否则「前缀是否存在」成为
        可测量的侧信道。
        """

    @abstractmethod
    def revoke(self, key_fp: str) -> None:
        """按指纹撤销一把 key（幂等）。撤销后 :meth:`resolve` 立即不再命中。"""

    def is_revoked(self, credential_id: str) -> bool:
        """凭据是否已撤销（供 PEP 在线复核缓存的 AuthContext，F05 §认证不变量 6）。

        ``credential_id`` 即 :meth:`resolve` 写进 ``AuthContext`` 的指纹。撤销后返回
        ``True``；未撤销或本注册表不认识该指纹返回 ``False``。

        非 abstract：支持撤销的后端（如 :class:`~...memory_key_store.InMemoryKeyStore`）
        覆盖之；不跟踪撤销状态的后端继承本默认实现，在被查询时 fail-closed 抛错，
        而不是静默返回「未撤销」把撤销后凭据放行。``ApiKeyAuthenticator`` 在认证期
        校验本方法已被覆盖，第三方缺实现会在签发上下文前就失败，而非首个授权请求
        500。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持凭据撤销查询")

    @abstractmethod
    def get_role(self, actor: Scope) -> Role | None:
        """查主体的服务端注册角色；未注册返回 ``None``。

        TRUSTED 模式据此实现「role 不从 header 读」（§2.2.2 关键设计）——网关说
        「你是谁」，框架自己查「你能干什么」。这样即使网关被攻破或误配，也无法
        任意提权。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""
