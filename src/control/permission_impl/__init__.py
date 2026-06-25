"""permission_impl 实现集：工厂 PermissionProducer + 各实现。

import 各实现模块即触发其 ``@PermissionProducer.register(...)`` 自注册；本包只对外暴露工厂 PermissionProducer。
"""

from importlib import import_module

from control.permission import PermissionProducer

import_module(".allow_all_permission_manager", __name__)

__all__ = ["PermissionProducer"]
