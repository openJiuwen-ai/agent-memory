# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
""":class:`~api.memory_api.MemoryAPI` 的实现集 + 单进程参考装配。"""

from .assembly import (
    AUTO_SECURITY_RUNTIME,
    MaintenancePorts,
    ManagedMemoryRuntime,
    ManagedServerRuntime,
    MemoryRuntime,
    ServerRuntime,
    assemble,
    assemble_maintenance_ports,
    assemble_managed_runtime,
    assemble_managed_server_runtime,
    assemble_runtime,
    assemble_server_runtime,
)
from .local_memory_api import LocalMemoryAPI

__all__ = [
    "LocalMemoryAPI",
    "MemoryRuntime",
    "assemble",
    "MaintenancePorts",
    "assemble_maintenance_ports",
    "ManagedMemoryRuntime",
    "ManagedServerRuntime",
    "assemble_managed_runtime",
    "assemble_managed_server_runtime",
    "assemble_runtime",
    "ServerRuntime",
    "assemble_server_runtime",
    "AUTO_SECURITY_RUNTIME",
]
