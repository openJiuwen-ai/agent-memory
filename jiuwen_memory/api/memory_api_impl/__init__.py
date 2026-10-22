# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
""":class:`~api.memory_api.MemoryAPI` 的实现集 + 单进程参考装配。"""

from .assembly import (
    AUTO_SECURITY_RUNTIME,
    MemoryRuntime,
    ServerRuntime,
    assemble,
    assemble_runtime,
    assemble_server_runtime,
)
from .local_memory_api import LocalMemoryAPI

__all__ = [
    "LocalMemoryAPI",
    "MemoryRuntime",
    "assemble",
    "assemble_runtime",
    "ServerRuntime",
    "assemble_server_runtime",
    "AUTO_SECURITY_RUNTIME",
]
