"""authorization_impl 实现集：AuthorizationProducer + 各实现。

import 各实现模块即触发其 ``@AuthorizationProducer.register(...)`` 自注册；本包只
对外暴露工厂。Grant/Delegation 的存储实现（``memory_stores``、``sqlite_stores``）也在
这里 import——Authorizer 的 ``_build`` 通过 ``GrantStoreProducer.dep`` 引用它们的
注册名，存储模块没被 import 过就等于那些名字不存在。
"""

from importlib import import_module

from common.security.authorization.base import AuthorizationProducer

import_module(".memory_stores", __name__)
import_module(".sqlite_stores", __name__)
import_module(".allow_all_authorizer", __name__)
import_module(".standard_authorizer", __name__)
import_module(".routing_authorizer", __name__)

__all__ = ["AuthorizationProducer"]
