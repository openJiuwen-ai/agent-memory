# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scene 构建测试依赖，公开字段用于记录模型输入和故障注入。"""

from copy import deepcopy
from dataclasses import dataclass, field

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder import Embedder
from jiuwen_memory.common.llm import LLM
from jiuwen_memory.common.type_def import ChatMessage, ContentLayers, HierarchyKind, MemoryUnit
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeProfile
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.construction.hierarchy_composer_impl.time_pipeline import (
    TimeSpanMergerOptions,
    build_time_span_parent,
)
from jiuwen_memory.construction.layer_annotator import LayerAnnotator
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE, make_leaf


@dataclass
class RecordingEmbedder(Embedder):
    vectors: list[list[float]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.EMBEDDER

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(deepcopy(texts))
        return deepcopy(self.vectors)

    @staticmethod
    def dimension() -> int:
        return 2

    @staticmethod
    def health() -> None:
        return None


@dataclass
class RecordingLLM(LLM):
    responses: list[str | Exception] = field(default_factory=list)
    calls: list[list[ChatMessage]] = field(default_factory=list)

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.LLM

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        self.calls.append(deepcopy(messages))
        if not self.responses:
            raise RuntimeError("test LLM response queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @staticmethod
    def health() -> None:
        return None


class RecordingAnnotator(LayerAnnotator):
    """观察只接收父，另可模拟越权修改字段或运行失败。"""

    def __init__(self, failure: bool = False) -> None:
        super().__init__(layers_threshold=0)
        self.calls: list[list[MemoryUnit]] = []
        self.failure = failure

    def annotate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        self.calls.append(deepcopy(units))
        for unit in units:
            unit.layers = ContentLayers(l0="摘要", l1="更详细的要点摘要")
            unit.segments[0].content = "插件不应修改正文"
            unit.hierarchy.child_ids = []
        if self.failure:
            raise RuntimeError("test annotation failure")
        return units

    @staticmethod
    def health() -> None:
        return None


def make_span(uid: str, minute: float = 0, duration: float = 0) -> MemoryUnit:
    """构建确定性 time_span 输入，固定 id 便于断言分组。"""
    span = build_time_span_parent(
        [make_leaf(f"{uid}-leaf", minute, duration=duration)],
        tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions(),
    )
    span.id = uid
    return span


def summary_profiles(**overrides: str) -> dict[HierarchyKind, HierarchyComposeProfile]:
    """真实配置解析，默认两层父都开启模型摘要。"""
    merger = {"summary_mode": "llm", "gap_seconds": "60"}
    merger.update(overrides)
    return build_profiles({"time": {
        "parent_roles": ["time_span", "scene"],
        "stage_options": {
            "TimeSpanMerger": merger,
            "SceneSegmenter": {"summary_mode": "llm", "similarity_threshold": "0.5"},
        },
    }})
