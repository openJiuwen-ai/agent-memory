# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permission_impl 实现集：工厂 PermissionProducer + 各实现。

import 各实现模块即触发其 ``@PermissionProducer.register(...)`` 自注册；
本包只对外暴露工厂 PermissionProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.control.permission import PermissionProducer

import_optional(".allow_all_permission_manager", __name__)
import_optional(".routing_permission_manager", __name__)
import_optional(".sqlite_permission_manager", __name__)
import_optional(".space_aware_permission_manager", __name__)

__all__ = ["PermissionProducer"]
