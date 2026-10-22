# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式 TIME 构建的可选装配；绑定同源依赖，不重建现有演进流水线。"""

from jiuwen_memory.common.llm import LLM
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposer,
    HierarchyComposerProducer,
)
from jiuwen_memory.construction.index_builder import IndexBuilder


def optional_hierarchy_composer(
    config, *, index_builder: IndexBuilder | None = None, llm: LLM | None = None,
) -> HierarchyComposer | None:
    """显式依赖优先；否则绑定内置 TIME 预设，构建校验延迟到真正使用时。"""
    if "hierarchy_composer" in config.params:
        configured = config.params["hierarchy_composer"]
        if configured is None or configured == "":
            return None
        return HierarchyComposerProducer.dep(config, "hierarchy_composer")
    profiles = config.get("hierarchy_profiles")
    if profiles is None or index_builder is None or llm is None:
        return None
    from jiuwen_memory.common.embedder import DeferredEmbedder
    from jiuwen_memory.common.embedder.base import EmbedderProducer
    from jiuwen_memory.construction.lazy_hierarchy_composer import LazyHierarchyComposer

    return LazyHierarchyComposer(
        index_builder, llm,
        DeferredEmbedder(EmbedderProducer.bind_named("default", config.ctx)), profiles,
    )
