# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内置 TIME 构建预设：捕获本轮共享依赖，首次使用时校验并创建 Composer。"""

from copy import deepcopy
from threading import Lock

from jiuwen_memory.common.embedder import Embedder
from jiuwen_memory.common.llm import LLM
from jiuwen_memory.common.type_def import HierarchyKind
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeProfile,
    HierarchyComposer,
    HierarchyComposeRequest,
    HierarchyComposeResult,
)
from jiuwen_memory.construction.index_builder import IndexBuilder


class LazyHierarchyComposer(HierarchyComposer):
    """不持有 Factory 或配置环境，避免后续 assemble 改变构建依赖的来源。"""

    def __init__(self, index_builder: IndexBuilder, llm: LLM, embedder: Embedder,
                 profiles: object) -> None:
        self._index = index_builder
        self._llm = llm
        self._embedder = embedder
        self._profiles = deepcopy(profiles)
        self._delegate: HierarchyComposer | None = None
        self._lock = Lock()

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.HIERARCHY_COMPOSER

    @staticmethod
    def health() -> None:
        return None

    def _get(self) -> HierarchyComposer:
        with self._lock:
            if self._delegate is None:
                from jiuwen_memory.construction.hierarchy_composer_impl.default_composer import (
                    DefaultHierarchyComposer,
                )
                from jiuwen_memory.construction.hierarchy_composer_impl.parent_enrichment import (
                    HierarchyModelDependencies,
                )
                from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import (
                    build_profiles,
                )
                self._delegate = DefaultHierarchyComposer(
                    index_builder=self._index, profiles=build_profiles(self._profiles),
                    models=HierarchyModelDependencies(
                        embedder=self._embedder, llm=self._llm,
                    ),
                )
            return self._delegate

    def get_profile(self, kind: HierarchyKind) -> HierarchyComposeProfile | None:
        return self._get().get_profile(kind)

    def build(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        return self._get().build(request)

    def replace_in_span(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        return self._get().replace_in_span(request)
