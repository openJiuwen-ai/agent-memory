# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""树构建算子实现，导入时注册 default 实现。"""

from .default_composer import DefaultHierarchyComposer
from .recursive_composer import RecursiveHierarchyComposer

__all__ = ["DefaultHierarchyComposer", "RecursiveHierarchyComposer"]
