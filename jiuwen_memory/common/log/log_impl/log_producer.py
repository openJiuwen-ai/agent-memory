"""LogProducer — 注册式工厂：管理 log_impl 下全部日志初始化实现（按 config 选）。

各实现在本包内以 ``@LogProducer.register("<名>")`` 自注册；builder 签名为
``(config) -> None``——执行日志初始化而非返回实例。新增实现只写实现 + 自注册 +
在 ``__init__`` 重导出，无需改本文件或 build_kernel。
"""

from __future__ import annotations

from jiuwen_memory.common.factory.factory import Factory


class LogProducer(Factory):
    """注册式工厂；``name`` 即实现名。"""

    TOP_NAME = "log"
