# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""audit_impl 实现集：工厂 AuditProducer + 各实现。

import 各实现模块即触发其 ``@AuditProducer.register(...)`` 自注册；
本包只对外暴露工厂 AuditProducer。
"""

import logging
from importlib import import_module

from jiuwen_memory.common.audit.base import AuditProducer

try:
    import_module(".in_memory_audit_logger", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)
try:
    import_module(".sqlite_audit_logger", __name__)
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["AuditProducer"]
