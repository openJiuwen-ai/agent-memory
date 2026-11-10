# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Factories for independent memory scenes; shared interfaces have no concrete imports."""
from collections.abc import Callable

from jiuwen_memory_adapter.jiuwenswarm.coding_memory import CodingMemoryScene
from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemoryScene
from jiuwen_memory_adapter.jiuwenswarm.work_memory import WorkMemoryScene

SCENE_FACTORIES: dict[str, Callable[[str], MemoryScene]] = {
    "agent": lambda _project: WorkMemoryScene(),
    "code": CodingMemoryScene,
}


def create_memory_scene(name: str, project: str) -> MemoryScene:
    """Resolve once at construction; unknown scenes never fall back to Work."""
    try:
        factory = SCENE_FACTORIES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown memory scene: {name}") from exc
    return factory(project)
