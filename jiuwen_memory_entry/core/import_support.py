# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""接入形态侧的导入容错样板（HTTP / MCP / CLI 共用）。

各 surface 的入口在启动期要 import 共享应用核（``config_loader`` / ``profiles`` /
``server`` / ``auth_middleware``）与内核装配面。这些是**必需**依赖：缺任何一个服务都
起不来，故只记录一次 warning（带目标模块名，便于区分"没装依赖"与"内部导错名字"）后
原样抛出，绝不静默降级成"看起来起来了但少功能"。

Access 侧只允许依赖 ``jiuwen_memory.api``（见 ``tests/unit/api/test_access_api_boundary.py``），
因此本模块不复用 ``jiuwen_memory.common._import_support``，各自独立。
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

logger = logging.getLogger("agent-memory.access")

__all__ = ["import_required", "import_required_attr"]


def import_required(module_name: str, package: str | None = None) -> ModuleType:
    """导入启动必需模块；失败记录缺失目标后原样抛出。"""
    try:
        return importlib.import_module(module_name, package)
    except ImportError as error:
        logger.warning("required import failed: %s: %s", module_name, error)
        raise


def import_required_attr(module_name: str, attribute: str) -> Any:
    """取必需模块的某个属性（等价于 ``import_module(name).attr``）。"""
    return getattr(import_required(module_name), attribute)
