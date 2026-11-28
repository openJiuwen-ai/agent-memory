# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``Evolver.evolve_grouped`` 的来源归属与可重放边界（F10 决策 6、8）。"""

from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.abstractor import Abstractor
from jiuwen_memory.construction.associator import Associator
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import Dedup
from jiuwen_memory.construction.evolver import ExtractNotCommittedError
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
    ForwardIndexBuilder,
)
from jiuwen_memory.storage.kv import KVStore
from tests.conftest import make_storage

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="prod", user="u1")


def _unit(
    unit_id: str,
    content: str,
    provenance: list[str] | None = None,
    *,
    when: str = "2024-01-01T00:00:00+00:00",
) -> MemoryUnit:
    from datetime import datetime

    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(t_message=datetime.fromisoformat(when)),
        provenance=list(provenance or []),
    )


class _Extractor(Extractor):
    def __init__(self, derived: list[MemoryUnit], *, fail: bool = False) -> None:
        self._derived = derived
        self._fail = fail
        self.calls = 0

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units, *, context=None):
        self.calls += 1
        if self._fail:
            raise RuntimeError("extractor exploded")
        return [unit for unit in self._derived]


class _Dedup(Dedup):
    def __init__(self, hits: dict[str, list[tuple[MemoryUnit, float]]]) -> None:
        from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

        super().__init__(InMemoryKVStore())
        self._hits = hits

    def operator_type(self) -> OperatorType:
        return OperatorType.DEDUP

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit):
        return list(self._hits.get(candidate.id, []))


class _NoopAbstractor(Abstractor):
    def operator_type(self) -> OperatorType:
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        return None

    def abstract(self, units):
        return []


class _NoopAssociator(Associator):
    def operator_type(self) -> OperatorType:
        return OperatorType.ASSOCIATOR

    def health(self) -> None:
        return None

    def associate(self, units):
        return []


class _JsonLLM(LLM):
    def __init__(self, response: str) -> None:
        self._response = response

    def plugin_type(self):
        from jiuwen_memory.common.base import PluginType

        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages, **kwargs) -> str:
        return self._response


class _ExplodingBuilder(ForwardIndexBuilder):
    """先落盘再抛错：模拟「落盘之后」的失败。"""

    def build(self, units, *, mode=None) -> None:  # type: ignore[override]
        super().build(units)
        raise RuntimeError("index write blew up after commit")


def _evolver(
    kv: KVStore,
    *,
    derived: list[MemoryUnit],
    hits: dict[str, list[tuple[MemoryUnit, float]]],
    llm: LLM | None = None,
    extractor_fails: bool = False,
    builder=None,
) -> OrchestratingEvolver:
    manager = make_storage(kv=kv)
    index = builder or ForwardIndexBuilder(manager)
    return OrchestratingEvolver(
        extractor=_Extractor(derived, fail=extractor_fails),
        abstractor=_NoopAbstractor(),
        associator=_NoopAssociator(),
        index_builder=index,
        storage=manager,
        message_store=kv,
        dedup=_Dedup(hits),
        llm=llm or _JsonLLM("[]"),
        dedup_medium_similarity=0.7,
        dedup_high_similarity=1.0,
    )


def test_sources_cover_add_and_update() -> None:
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    manager = make_storage(kv=kv)
    existing = _unit("existing-1", "用户偏好 Python")
    ForwardIndexBuilder(manager).build([existing])

    added = _unit("added-1", "用户居住杭州", provenance=["src-1"])
    updated = _unit(
        "cand-1",
        "用户偏好 Python 3",
        provenance=["src-1"],
        when="2024-02-01T00:00:00+00:00",
    )
    source = _unit("src-1", "我住杭州，喜欢 Python 3")

    # 候选 cand-1 命中已有记忆，交给 LLM 判为 UPDATE；候选 added-1 无命中。
    evolver = _evolver(
        kv,
        derived=[added, updated],
        hits={"cand-1": [(existing, 0.8)]},
        llm=_JsonLLM(
            json.dumps(
                [{"candidate_id": "c1", "decision": "update", "existing_id": "e1"}]
            )
        ),
    )

    grouped = evolver.evolve_grouped([source])

    assert grouped.result.created_ids == ["added-1"], "ADD 产出的派生记忆应落盘"
    assert grouped.result.updated_ids == ["existing-1"], "UPDATE 回写已有记忆 id"
    assert grouped.sources["added-1"] == ["src-1"], "ADD 的来源按 provenance 归属"
    assert grouped.sources["existing-1"] == ["src-1"], (
        "UPDATE 的 key 是目标记忆 id，来源取去重前候选的 provenance"
    )


def test_failure_before_persist_raises_not_committed() -> None:
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    evolver = _evolver(
        kv,
        derived=[],
        hits={},
        extractor_fails=True,
    )

    with pytest.raises(ExtractNotCommittedError):
        evolver.evolve_grouped([_unit("src-1", "任何内容")])

    assert list(kv.scan(_SCOPE, prefix="/memory/")) == [], (
        "抽取阶段失败时不得留下任何派生记忆"
    )


def test_failure_after_persist_is_not_wrapped() -> None:
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    manager = make_storage(kv=kv)
    evolver = _evolver(
        kv,
        derived=[_unit("added-1", "用户居住杭州", provenance=["src-1"])],
        hits={},
        builder=_ExplodingBuilder(manager),
    )

    with pytest.raises(RuntimeError, match="index write blew up"):
        evolver.evolve_grouped([_unit("src-1", "我住杭州")])

    assert evolver is not None, "落盘后的失败必须保留原异常类型，不能包装成可重放错误"


def test_replay_messages_idempotent() -> None:
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    kv = InMemoryKVStore()
    evolver = _evolver(
        kv,
        derived=[_unit("added-1", "用户居住杭州", provenance=["src-1"])],
        hits={},
    )
    source = _unit("src-1", "我住杭州")

    evolver.evolve_grouped([source])
    first = len(evolver._list_messages(source.scope))
    evolver.evolve_grouped([source])
    second = len(evolver._list_messages(source.scope))

    assert second == first, "同一批原文重放不得在 /messages/ 里重复写入"
