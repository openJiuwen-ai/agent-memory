"""authenticator_impl 实现集：工厂 AuthProducer + 各实现。

import 各实现模块即触发其 ``@AuthProducer.register(...)`` 自注册；
本包只对外暴露工厂 AuthProducer。
"""

from importlib import import_module

from security.authenticator import AuthProducer

import_module(".api_key_authenticator", __name__)
import_module(".dev_authenticator", __name__)
import_module(".trusted_authenticator", __name__)

__all__ = ["AuthProducer"]
