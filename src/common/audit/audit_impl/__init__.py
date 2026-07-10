"""audit_impl 实现集：工厂 AuditProducer + 各实现。

import 各实现模块即触发其 ``@AuditProducer.register(...)`` 自注册；
本包只对外暴露工厂 AuditProducer。
"""

from importlib import import_module

from common.audit.base import AuditProducer

import_module(".in_memory_audit_logger", __name__)
import_module(".sqlite_audit_logger", __name__)

__all__ = ["AuditProducer"]
