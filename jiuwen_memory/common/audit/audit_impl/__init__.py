# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""audit_impl 实现集：工厂 AuditProducer + 各实现。

import 各实现模块即触发其 ``@AuditProducer.register(...)`` 自注册；
本包只对外暴露工厂 AuditProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.audit.base import AuditProducer

import_optional(".in_memory_audit_logger", __name__)
import_optional(".sqlite_audit_logger", __name__)

__all__ = ["AuditProducer"]
