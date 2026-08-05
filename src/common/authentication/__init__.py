"""认证能力的抽象接口、数据类型与注册入口。"""

from .base import Authenticator, AuthProducer
from .binding import check_dev_binding
from .bootstrap import register_authentication
from .types import AuthMode, Credentials

__all__ = [
    "Authenticator",
    "AuthMode",
    "AuthProducer",
    "Credentials",
    "check_dev_binding",
    "register_authentication",
]
