"""authentication_impl 实现集：工厂 AuthProducer / KeyStoreProducer + 各实现。

import 各实现模块即触发其 ``@AuthProducer.register(...)`` /
``@KeyStoreProducer.register(...)`` 自注册；本包只对外暴露两个工厂。

key_store 先于 authenticator import：后者的 ``_build`` 通过
``KeyStoreProducer.dep(..., default="memory")`` 引用前者的注册名。注册发生在
import 期而装配发生在 build 期，顺序其实不影响正确性，但保持依赖方向可读。
"""

from importlib import import_module

from common.security.authentication.base import AuthProducer
from common.security.authentication.key_store import KeyStoreProducer

import_module(".memory_key_store", __name__)
import_module(".api_key_authenticator", __name__)
import_module(".dev_authenticator", __name__)
import_module(".trusted_authenticator", __name__)

__all__ = ["AuthProducer", "KeyStoreProducer"]
