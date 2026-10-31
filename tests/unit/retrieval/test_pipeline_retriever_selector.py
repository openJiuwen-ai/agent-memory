"""PipelineRetriever 的选取阶段接线（mem2.0 §2.4）。

选取算子接在阈值过滤（[8]）之后、截断 top_k（[9]）之前；选取换上的条目副本
必须进得到返回结果——披露读的条目字典要随之重建。未配置选取算子时链路行为
不变。本文件用假 storage/parser/fuser/discloser 与打标记的假 selector 验证接线，
不依赖真实召回后端。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import (
    MemoryTier,
    Modality,
    ParsedQuery,
    RecallBatch,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredUnit,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory import MemoryUnit
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RetrievalQuery

pytestmark = pytest.mark.unit


def _unit(entry_id: str) -> MemoryUnit:
    return MemoryUnit(
        id=entry_id,
        scope=Scope(org="o1", space="shared-experience"),
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=f"content-{entry_id}", source=Modality.TEXT)],
        temporal=Temporal(t_message=None),
        system_metadata={"memory_type": "experience", "category": "cat-a"},
    )


class _FakeStorage:
    """recall 返回打分候选，get 按 id 物化；与真实后端无关的最小数据面。"""

    def __init__(self, scored: dict[str, float]) -> None:
        self._units = {entry_id: _unit(entry_id) for entry_id in scored}
        self._scored = scored

    @staticmethod
    def preferred_retrieval_pipeline() -> RetrievalPipeline:
        return RetrievalPipeline.RECALL_GET_RANK

    def recall(self, scope, query, *, channels, recall_limit):
        candidates = [
            ScoredUnit(unit_id=entry_id, score=score, channel=RecallChannel.KEYWORD)
            for entry_id, score in self._scored.items()
        ]
        return RecallResult(
            batches=[RecallBatch(RecallChannel.KEYWORD, "fake", candidates)]
        )

    def get(self, scope, unit_ids):
        return [self._units[uid] for uid in unit_ids if uid in self._units]


class _ScriptedParser:
    @staticmethod
    def parse(query) -> ParsedQuery:
        return ParsedQuery(raw="hello world", channels=[RecallChannel.KEYWORD])


class _PassthroughFuser:
    @staticmethod
    def fuse(query, candidates):
        return [c for batch in candidates for c in batch]


class _MarkingSelector:
    """记录收到的候选与 top_k；给每个返回候选换带引用值的条目副本。"""

    def __init__(self) -> None:
        self.received_ids: list[str] = []
        self.received_top_k: int | None = None

    @staticmethod
    def operator_type():  # 接口要求；本测试只经 PipelineRetriever 间接调用
        return RetrievalOperatorType.SELECTOR

    @staticmethod
    def health() -> None:
        return None

    def select(self, survivors, query):
        self.received_ids = [c.unit_id for c in survivors]
        self.received_top_k = query.top_k
        out = []
        for candidate in survivors:
            meta = {**candidate.unit.system_metadata, "usage_ref": "0.n"}
            out.append(replace(candidate, unit=replace(candidate.unit, system_metadata=meta)))
        return out


class _RecordingDiscloser:
    """记下披露读到的条目字典与最终列表，返回透传 items。"""

    def __init__(self) -> None:
        self.seen_units: dict[str, MemoryUnit] = {}
        self.seen_ids: list[str] = []

    def disclose(self, query, candidates, units, level, max_tokens=None):
        self.seen_units = units
        self.seen_ids = [c.unit_id for c in candidates]
        return [
            type(
                "Item",
                (),
                {
                    "unit_id": c.unit_id,
                    "content": units[c.unit_id].content,
                    "system_metadata": dict(units[c.unit_id].system_metadata),
                },
            )()
            for c in candidates
        ]


def _retriever(storage, *, selector) -> tuple[PipelineRetriever, _RecordingDiscloser]:
    discloser = _RecordingDiscloser()
    retriever = PipelineRetriever(
        parser=_ScriptedParser(),
        fuser=_PassthroughFuser(),
        discloser=discloser,
        unit_reader=None,
        domain_store=storage,
        selector=selector,
    )
    return retriever, discloser


def test_selector_receives_threshold_survivors() -> None:
    """select 收到的是过阈值后的候选：零分候选不进选取。"""
    storage = _FakeStorage({"m-1": 1.0, "m-2": 0.5, "dead": 0.0})
    selector = _MarkingSelector()
    retriever, _ = _retriever(storage, selector=selector)

    retriever.retrieve(Scope(), RetrievalQuery(text="hello world"))

    assert selector.received_ids == ["m-1", "m-2"], "零分候选被阈值滤掉，不进选取"


def test_selector_copies_reach_disclosure_and_result() -> None:
    """选取换上的副本进披露与返回结果（条目字典重建生效）。"""
    storage = _FakeStorage({"m-1": 1.0, "m-2": 0.5})
    selector = _MarkingSelector()
    retriever, discloser = _retriever(storage, selector=selector)

    result = retriever.retrieve(Scope(), RetrievalQuery(text="hello world"))

    assert discloser.seen_ids == ["m-1", "m-2"], "披露读选取后的顺序"
    for entry_id in ("m-1", "m-2"):
        assert discloser.seen_units[entry_id].system_metadata["usage_ref"] == "0.n", (
            f"披露读到的 {entry_id} 是带引用值的副本"
        )
    assert all(item.system_metadata["usage_ref"] == "0.n" for item in result.items), (
        "返回结果带引用值（副本未在重建中丢失）"
    )


def test_top_k_truncates_after_select() -> None:
    """截断发生在选取之后：选取返回多于 top_k 的条目时只留前 top_k。"""
    storage = _FakeStorage({"m-1": 1.0, "m-2": 0.5})
    selector = _MarkingSelector()
    retriever, _ = _retriever(storage, selector=selector)

    result = retriever.retrieve(Scope(), RetrievalQuery(text="hello world", top_k=1))

    assert [item.unit_id for item in result.items] == ["m-1"], "选取后截断 top_k"


def test_no_selector_keeps_behavior() -> None:
    """未配置选取算子：链路行为不变，条目元数据原样（无引用值）。"""
    storage = _FakeStorage({"m-1": 1.0})
    retriever, discloser = _retriever(storage, selector=None)

    result = retriever.retrieve(Scope(), RetrievalQuery(text="hello world"))

    assert [item.unit_id for item in result.items] == ["m-1"], "正常返回"
    assert "usage_ref" not in result.items[0].system_metadata, "无选取算子不注入引用值"
    assert "usage_ref" not in discloser.seen_units["m-1"].system_metadata, (
        "无选取算子时条目对象不经副本替换"
    )


def test_select_stage_recorded_in_trajectory() -> None:
    """配置了选取算子时轨迹多出 select 阶段；未配置时没有。"""
    storage = _FakeStorage({"m-1": 1.0})
    selector = _MarkingSelector()
    retriever, _ = _retriever(storage, selector=selector)
    with_select = retriever.retrieve(
        Scope(), RetrievalQuery(text="hello world", with_trajectory=True)
    )
    stages = [step.stage for step in with_select.trajectory]
    assert "select" in stages, "轨迹记录选取阶段"

    retriever_none, _ = _retriever(_FakeStorage({"m-1": 1.0}), selector=None)
    without_select = retriever_none.retrieve(
        Scope(), RetrievalQuery(text="hello world", with_trajectory=True)
    )
    assert "select" not in [step.stage for step in without_select.trajectory], (
        "未配置选取算子时无 select 阶段"
    )
