# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""控制层应用端口（B-03）：按用例包装已有算子，不是新算子。

四个服务均非 ``ControlOperator``——无 Producer 注册、实现不可替换、不访问存储与模型，
因此不进工厂，也不由 :func:`control.bootstrap.register_controllers` 触发注册。
带实现的模块收在子包而不放顶层，见 ``control/AGENTS.md``「文件关系」第一条。

共同形态：**裁决留 PEP，已鉴权之后的数据面/治理/Space 删除事务落本包**。服务不接收
``identity``、不调用 ``PermissionManager``、不抛权限异常。API 仍负责 DTO/Scope 适配、
PEP、路由谓词回注、逐条结果鉴权、membership 失效和入口审计。

消费方写 ``from jiuwen_memory.control.application import MemoryCommandService`` 等
具名导入；SDK/HTTP/MCP 经 ``LocalMemoryAPI`` 共用同一组端口。
"""

from __future__ import annotations

from .command import MemoryCommandService
from .governance_service import GovernanceService
from .query import MemoryQueryService
from .space_lifecycle import SpaceLifecycleService

__all__ = [
    "GovernanceService",
    "MemoryCommandService",
    "MemoryQueryService",
    "SpaceLifecycleService",
]
