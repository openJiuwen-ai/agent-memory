# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permission_impl 实现集：工厂 PermissionProducer + 各实现。

import 各实现模块即触发其 ``@PermissionProducer.register(...)`` 自注册；
本包只对外暴露工厂 PermissionProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.control.permission import PermissionProducer

try:
    import_module(".allow_all_permission_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".routing_permission_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".sqlite_permission_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".space_aware_permission_manager", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["PermissionProducer"]
