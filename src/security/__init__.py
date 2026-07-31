"""安全层（认证）：把凭据校验成可信身份。

三道防线（`docs/features/common/F04-security-interfaces-and-encryption.md` §1）中的
第①道。授权（②）在 ``src/control/permission.py``，本模块只回答「你是谁」，
不回答「你能做什么」。

限流（§8.1）也在本模块：它保护的是认证本身——Argon2 verify 是 CPU/内存
密集操作，无限制触发能把进程打挂。故限流是认证的前置门，不是独立关注点。

对外只暴露抽象与工厂；实现在 ``authenticator_impl/`` / ``key_store_impl/`` /
``rate_limit_impl/`` 下自注册，由 :func:`security.bootstrap.register_security` 触发。
"""

from .authenticator import Authenticator, AuthProducer
from .binding import check_dev_binding
from .bootstrap import register_security
from .key_store import KeyStoreProducer, PrincipalKeyStore, fingerprint, generate_api_key
from .rate_limit import RateLimiter, RateLimitProducer
from .types import AuthMode, Credentials

__all__ = [
    "Authenticator",
    "AuthProducer",
    "AuthMode",
    "Credentials",
    "PrincipalKeyStore",
    "KeyStoreProducer",
    "RateLimiter",
    "RateLimitProducer",
    "fingerprint",
    "generate_api_key",
    "check_dev_binding",
    "register_security",
]
