# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SkillAbstractor 测试（mem2.0 §2.5.3-2.5.5）。

覆盖：dedup_products 契约、分组（分桶/连边约束/切开）、条件检查
（五条件）、幂等（确定性条目 id）、生成全链路（草稿字段）、校验（技能名/
脱敏/命令覆盖）、SKILL.md 渲染、auto 发布附加条件、运行摘要补写、配置
降级。

模型与向量化用替身驱动：ScriptedLLM 按序返回预置 JSON，TagEmbedder 按正文
中的簇标签产 one-hot 向量（同标签余弦 1、异标签 0）——分组结果可由测试
文本直接控制。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.experience import (
    EVIDENCE_EXTERNAL,
    EVIDENCE_MODEL_ONLY,
    OUTCOME_SUCCESS,
    ReproductionEvent,
    UtilityStore,
)
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.abstractor import Abstractor
from jiuwen_memory.construction.associator import Associator
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import Dedup
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
)
from jiuwen_memory.construction.experience import (
    CuratedSpaceConfig,
    ExecutionTrajectoryRecord,
    ExperienceConfig,
    TrajectoryStore,
)
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# 常量与替身
# ---------------------------------------------------------------------------

EXP_SPACE = "shared-experience"
EXP_SCOPE = Scope(org="acme", space=EXP_SPACE)

_DEFAULT_SCOPE = Scope(org="test", user="alice", agent="a1", session="s1")


class _StubAbstractor(Abstractor):
    """Mock Abstractor：返回预置产物；dedup_products 可按实现声明覆盖。"""

    def __init__(
        self, products: list[MemoryUnit], dedup_products: bool = True
    ) -> None:
        self._products = products
        self.dedup_products = dedup_products

    def operator_type(self) -> OperatorType:
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        return None

    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        return list(self._products)


class _RecordingIndexBuilder(IndexBuilder):
    """Mock IndexBuilder：记录 build 收到的单元（落盘即 build，不写真源）。"""

    def __init__(self) -> None:
        self.built: list[MemoryUnit] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        self.built.extend(units)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        pass

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        pass

    def rebuild(self) -> None:
        pass


class _CountingDedup(Dedup):
    """Mock Dedup：只记 recall 次数，恒返回空（无命中 → 走 ADD 分支）。"""

    def __init__(self) -> None:
        # 基类 __init__ 需要 kv（recall 被覆写恒返空，kv 不会被真正使用），
        # 传 InMemoryKVStore 满足构造契约（G.CLS.01 子类 __init__ 调父类）。
        super().__init__(InMemoryKVStore())
        self.recall_count = 0

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER  # 去重召回服务于 evolver，复用其算子类型

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        self.recall_count += 1
        return []


class ScriptedLLM(LLM):
    """按序返回预置响应；记录每次收到的 messages。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list] = []

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages, **options):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("ScriptedLLM: no scripted response left")
        return self._responses.pop(0)


class TagEmbedder(Embedder):
    """按正文中的簇标签产 one-hot 向量：同标签余弦 1、异标签 0。"""

    TAGS = ("clusterA", "clusterB", "clusterC")

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def dimension(self) -> int:
        return len(self.TAGS)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * len(self.TAGS)
        for i, tag in enumerate(self.TAGS):
            if tag in text:
                vector[i] = 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _make_unit(
    unit_id: str,
    content: str,
    scope: Scope = _DEFAULT_SCOPE,
    tier: MemoryTier = MemoryTier.PROCEDURAL,
) -> MemoryUnit:
    """创建测试 MemoryUnit（技能草稿默认 PROCEDURAL）。"""
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        tier=tier,
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=LifecycleState.ACTIVE,
        temporal=Temporal(),
    )


def _make_evolver(abstractor: Abstractor) -> tuple[
    OrchestratingEvolver, _RecordingIndexBuilder, _CountingDedup
]:
    """创建 CONSOLIDATE 测试用 evolver（extractor/associator/llm 不被触碰）。"""
    index = _RecordingIndexBuilder()
    dedup = _CountingDedup()
    storage = MagicMock()
    storage.has_graph.return_value = False
    evolver = OrchestratingEvolver(
        extractor=MagicMock(spec=Extractor),
        abstractor=abstractor,
        associator=MagicMock(spec=Associator),
        index_builder=index,
        storage=storage,
        message_store=MagicMock(),
        dedup=dedup,
        llm=MagicMock(),
    )
    return evolver, index, dedup


def _entry(
    entry_id: str,
    *,
    category: str = "deploy",
    tier: MemoryTier = MemoryTier.SEMANTIC,
    cluster: str = "clusterA",
    metadata: dict | None = None,
    content: str = "",
    scope: Scope = EXP_SCOPE,
) -> MemoryUnit:
    """经验条目（cluster 标签写进正文，控制 TagEmbedder 的相似度）。"""
    system_metadata = {"category": category}
    system_metadata.update(metadata or {})
    return MemoryUnit(
        id=entry_id,
        scope=scope,
        tier=tier,
        lifecycle=LifecycleState.ACTIVE,
        segments=[Segment(content=content or f"{cluster} 条目 {entry_id}")],
        temporal=Temporal(),
        system_metadata=system_metadata,
    )


def _tip(
    entry_id: str,
    *,
    condition: str = "服务启动找不到配置",
    capability: str = "cp",
    action: str = "复制示例配置",
    category: str = "deploy",
    cluster: str = "clusterA",
    scope: Scope = EXP_SCOPE,
) -> MemoryUnit:
    """三段齐全的条件动作。"""
    return _entry(
        entry_id,
        tier=MemoryTier.PROCEDURAL,
        category=category,
        cluster=cluster,
        metadata={"condition": condition, "capability": capability, "action": action},
        scope=scope,
    )


def _repro(
    store: UtilityStore,
    entry_id: str,
    author: str,
    trajectory_id: str,
    *,
    outcome: str = OUTCOME_SUCCESS,
    evidence: str = EVIDENCE_EXTERNAL,
    retrieved: list[str] | None = None,
    source_scope: list[str] | None = None,
    days_ago: float = 1,
    scope: Scope = EXP_SCOPE,
) -> None:
    """写一条复现事件（来源轨迹默认写在作者自己的用户空间）。"""
    occurred = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    store.write_reproduction_event(
        scope,
        entry_id,
        author,
        trajectory_id,
        0,
        ReproductionEvent(
            source_scope=source_scope
            if source_scope is not None
            else ["acme", "", f"user-{author}", "", ""],
            trajectory_id=trajectory_id,
            author=author,
            retrieved_ids=list(retrieved or []),
            outcome=outcome,
            evidence=evidence,
            occurred_at=occurred,
        ),
    )


def _trajectory(
    kv, author: str, trajectory_id: str, calls: list[tuple[str, str]]
) -> None:
    """在作者的用户空间写一条轨迹记录（messages 由工具调用序列合成）。"""
    messages: list[dict] = []
    for index, (name, primary) in enumerate(calls):
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": f"call{index}",
                "function": {
                    "name": name,
                    "arguments": json.dumps({"command": primary}),
                },
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"call{index}", "content": "ok",
        })
    TrajectoryStore(kv).save(
        Scope(org="acme", user=f"user-{author}"),
        author,
        ExecutionTrajectoryRecord(trajectory_id=trajectory_id, messages=messages),
    )


def _skill_unit(
    unit_id: str,
    name: str,
    *,
    version: int = 1,
    provenance: tuple[str, ...] = (),
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    cutoff: str = "",
    source_users: int = 0,
    content: str = "",
) -> MemoryUnit:
    """经验空间里已有的技能条目（v3 §2.5 技能与经验同空间）。"""
    return MemoryUnit(
        id=unit_id,
        scope=EXP_SCOPE,
        tier=MemoryTier.PROCEDURAL,
        lifecycle=lifecycle,
        segments=[Segment(content=content or f"# {name}")],
        provenance=list(provenance),
        tags=["draft"],
        temporal=Temporal(),
        system_metadata={
            "memory_type": "skill",
            "skill_name": name,
            "skill_version": version,
            "evidence_cutoff": cutoff,
            "source_users": source_users,
            "category": "deploy",
        },
    )


def _write_skill(kv, unit: MemoryUnit) -> None:
    kv.insert(EXP_SCOPE, memory_key(unit.id), dumps(unit))


def _write_run_summary(kv, content: str = "{}") -> MemoryUnit:
    """写一条运行摘要（模拟 Curator 本轮写的那条）。"""
    unit = MemoryUnit(
        id="run-summary-1",
        scope=EXP_SCOPE,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content)],
        tags=["run-summary"],
        temporal=Temporal(),
    )
    kv.insert(EXP_SCOPE, memory_key(unit.id), dumps(unit))
    return unit


def _make_abstractor(
    kv,
    *,
    llm: LLM | None = None,
    embedder: Embedder | None = None,
    release: str = "manual",
    repro_unit: str = "user",
    identifiers: list[str] | None = None,
    **overrides,
):
    from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
        SkillAbstractor,
    )

    defaults = dict(
        kv=kv,
        embedder=embedder,
        llm=llm,
        # 经验落点空间开启技能沉淀（v3 §2.5：技能草稿与来源经验同空间）。
        experience=ExperienceConfig(
            default_space=EXP_SPACE,
            curated_spaces=(
                CuratedSpaceConfig(
                    space=EXP_SPACE,
                    skill=True,
                    skill_release=release,
                    repro_unit=repro_unit,
                ),
            ),
        ),
        desensitize_identifiers=identifiers,
    )
    defaults.update(overrides)
    return SkillAbstractor(**defaults)


def _skill_json(
    name: str = "deploy-config-bootstrap",
    *,
    steps: tuple[str, ...] = ("复制示例配置", "运行 `python app.py`"),
    description: str = "启动缺配置的 Python 服务",
) -> str:
    return json.dumps(
        {
            "name": name,
            "description": description,
            "tools": ["bash"],
            "scenarios": "启动服务报配置文件缺失时",
            "preconditions": ["仓库有示例配置"],
            "steps": list(steps),
            "failures": ["端口被占用：改端口重启"],
        },
        ensure_ascii=False,
    )


def _seed_qualifying_group(kv, store: UtilityStore) -> list[MemoryUnit]:
    """标准通过组：2 条同簇条件动作 + 3 用户独立复现（2 成功、1 部分）。"""
    entries = [_tip("m-0107"), _tip("m-0113")]
    _repro(store, "m-0107", "d1", "t1")
    _repro(store, "m-0113", "d2", "t2")
    _repro(
        store, "m-0107", "d3", "t3",
        outcome="partial", evidence=EVIDENCE_MODEL_ONLY,
    )
    _trajectory(kv, "d1", "t1", [("bash", "cp config.example config.yaml")])
    _trajectory(kv, "d2", "t2", [("bash", "python app.py --debug")])
    return entries


def _group(abstractor, entries):
    """白盒触达分组（经小工具绕行受保护访问）。"""
    return getattr(abstractor, "_group")(entries)


def _load_repro_events(abstractor, scope):
    """白盒触达复现事件扫描。"""
    return getattr(abstractor, "_load_repro_events")(scope)


def _check_group(abstractor, round_, entries, events_by_entry, skills):
    """白盒触达成组条件核对。"""
    return getattr(abstractor, "_check_group")(round_, entries, events_by_entry, skills)


def _pick_trajectories(abstractor, events):
    """白盒触达轨迹选取（经小工具绕行受保护访问）。"""
    return getattr(abstractor, "_pick_trajectories")(events)


# ---------------------------------------------------------------------------
# dedup_products 契约
# ---------------------------------------------------------------------------


class TestDedupProducts:
    """技能类产物跳过语义去重（§2.5.3）：dedup_products=False 直接落盘。"""

    @staticmethod
    def test_default_true():
        """接口缺省值：未覆盖本属性的实现走语义去重。"""
        assert Abstractor.dedup_products is True, "缺省必须保持去重（向后兼容）"

    @staticmethod
    def test_false_skips_dedup_and_persists():
        """dedup_products=False：CONSOLIDATE 跳过 _dedup_batch 直接落盘。"""
        draft_a = _make_unit("skill-a", "技能 A 的 SKILL.md 内容")
        draft_b = _make_unit("skill-b", "技能 B 的 SKILL.md 内容")
        evolver, index, dedup = _make_evolver(
            _StubAbstractor([draft_a, draft_b], dedup_products=False)
        )

        result = evolver.evolve([_make_unit("e1", "经验条目 1")], EvolveMode.CONSOLIDATE)

        assert result.created_ids == ["skill-a", "skill-b"], "产物 id 原样入 created_ids"
        assert [u.id for u in result.created_units] == ["skill-a", "skill-b"]
        assert index.built == [draft_a, draft_b], "产物经 IndexBuilder.build 落盘"
        assert dedup.recall_count == 0, "跳过 _dedup_batch：不调 Dedup.recall"

    @staticmethod
    def test_true_goes_through_dedup():
        """对照：dedup_products=True（缺省）仍走去重。"""
        draft = _make_unit("skill-a", "技能 A 的 SKILL.md 内容")
        evolver, _, dedup = _make_evolver(_StubAbstractor([draft]))

        result = evolver.evolve([_make_unit("e1", "经验条目 1")], EvolveMode.CONSOLIDATE)

        assert dedup.recall_count == 1, "缺省走去重：Dedup.recall 逐条调用"
        assert result.created_ids == ["skill-a"], "recall 无命中 → ADD 落盘"


# ---------------------------------------------------------------------------
# SkillAbstractor
# ---------------------------------------------------------------------------


class TestGrouping:
    """第 1 步分组：类别分桶 → 连边 → 连通 → 切开。"""

    @staticmethod
    def test_different_categories_never_grouped():
        """不同类别的条目不进同组（分桶先于连边）。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=ScriptedLLM([]), embedder=TagEmbedder())
        entries = [_tip("e1", category="deploy"), _tip("e2", category="config")]
        groups = _group(abstractor, entries)
        assert sorted(len(g) for g in groups) == [1, 1], "不同类别各成一组"

    @staticmethod
    def test_capability_constraint_for_two_tips():
        """两条都是条件动作但能力名不同时不连边（即使正文相似）。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=ScriptedLLM([]), embedder=TagEmbedder())
        a = _tip("e1", capability="cp")
        b = _tip("e2", capability="docker")
        groups = _group(abstractor, [a, b])
        assert sorted(len(g) for g in groups) == [1, 1], "能力名不同不连边"

    @staticmethod
    def test_same_cluster_and_capability_grouped():
        """同簇且能力名相同的条件动作连成一组。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=ScriptedLLM([]), embedder=TagEmbedder())
        a = _tip("e1", capability="cp")
        b = _tip("e2", capability="cp")
        groups = _group(abstractor, [a, b])
        assert [len(g) for g in groups] == [2], "相似且能力名相同连成一组"

    @staticmethod
    def test_large_group_split_by_cap():
        """超过 cap 的组按相似度切开（贪心分片，每片 ≤ cap）。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(
            kv, llm=ScriptedLLM([]), embedder=TagEmbedder(), group_size_cap=3
        )
        entries = [_tip(f"e{i}") for i in range(7)]
        groups = _group(abstractor, entries)
        assert all(len(g) <= 3 for g in groups), "每片不超过 cap"
        assert sum(len(g) for g in groups) == 7, "分片不丢条目"


class TestConditionCheck:
    """第 2 步五条件（§2.5.3）：须同时满足。"""

    @staticmethod
    def _checked(kv, entries, *, repro_unit="user", skills=None):
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _Round,
        )

        abstractor = _make_abstractor(
            kv, llm=ScriptedLLM([]), embedder=TagEmbedder(), repro_unit=repro_unit
        )
        round_ = _Round(scope=EXP_SCOPE, release="manual", repro_unit=repro_unit)
        events_by_entry = _load_repro_events(abstractor, EXP_SCOPE)
        return _check_group(
            abstractor, round_, entries, events_by_entry, skills or []
        )

    @staticmethod
    def test_qualifying_group_passes():
        """标准组（3 用户独立复现、2 条强证据成功）通过。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        group = TestConditionCheck._checked(kv, entries)
        assert group is not None, "标准组应通过全部条件"
        assert group.new_evidence == 3, "无 prev 时新证据 = 全部复现事件数"

    @staticmethod
    def test_repro_floor_user_count():
        """user 计数：独立复现只覆盖 1 个用户时不通过。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        _repro(store, "e1", "d1", "t1")
        _repro(store, "e1", "d1", "t2")
        assert TestConditionCheck._checked(kv, entries) is None, "1 个用户不满足下限"

    @staticmethod
    def test_repro_floor_trajectory_count():
        """trajectory 计数：独立复现轨迹数 < 3 时不通过（哪怕用户足够）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for author, tid in [("d1", "t1"), ("d2", "t2")]:
            _repro(store, "e1", author, tid)
        checked = TestConditionCheck._checked(kv, entries, repro_unit="trajectory")
        assert checked is None, "2 条独立轨迹 < 3"

    @staticmethod
    def test_independent_repro_excludes_group_entries():
        """使用记录含本组经验的复现不算独立复现。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1"), _tip("e2")]
        _repro(store, "e1", "d1", "t1")
        _repro(store, "e2", "d2", "t2", retrieved=["e1"])
        checked = TestConditionCheck._checked(kv, entries)
        assert checked is None, "独立复现只剩 1 个用户（d2 的记录含组内经验 e1）"

    @staticmethod
    def test_reliable_success_required():
        """有成功但全是 model_only 证据（无强证据）时不通过。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for author, tid in [("d1", "t1"), ("d2", "t2"), ("d3", "t3")]:
            _repro(store, "e1", author, tid, evidence=EVIDENCE_MODEL_ONLY)
        assert TestConditionCheck._checked(kv, entries) is None

    @staticmethod
    def test_success_ratio_floor():
        """成功比例低于 0.5 时不通过（有强证据成功也不够）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        _repro(store, "e1", "d1", "t1", outcome=OUTCOME_SUCCESS)
        for author, tid in [("d2", "t2"), ("d3", "t3")]:
            _repro(store, "e1", author, tid, outcome="failure")
        assert TestConditionCheck._checked(kv, entries) is None, "1/3 成功 < 0.5"

    @staticmethod
    def test_new_evidence_required_after_prev():
        """prev 存在且无晚于其截止时间的复现事件时不通过。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1"), _tip("e2")]
        cutoff = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        for entry_id, author, tid in [
            ("e1", "d1", "t1"), ("e2", "d2", "t2"), ("e1", "d3", "t3"),
        ]:
            store.write_reproduction_event(
                EXP_SCOPE, entry_id, author, tid, 0,
                ReproductionEvent(
                    source_scope=["acme", "", f"user-{author}", "", ""],
                    trajectory_id=tid, author=author,
                    occurred_at=old,
                ),
            )
        prev = _skill_unit("s-1", "deploy-config", provenance=("e1",), cutoff=cutoff)
        checked = TestConditionCheck._checked(kv, entries, skills=[prev])
        assert checked is None, "无新证据"

    @staticmethod
    def test_rejected_prev_needs_two_more_users():
        """prev 被驳回时复现用户数须比驳回时至少多 2 个。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for author, tid in [("d1", "t1"), ("d2", "t2"), ("d3", "t3")]:
            _repro(store, "e1", author, tid)
        prev = _skill_unit(
            "s-1", "deploy-config", provenance=("e1",),
            lifecycle=LifecycleState.FORGOTTEN, source_users=2,
        )
        checked = TestConditionCheck._checked(kv, entries, skills=[prev])
        assert checked is None, "3 用户 < 驳回时 2 + 2"

    @staticmethod
    def test_baseline_blocks_low_validity():
        """组内合计有效性低于同类别中位数时不通过。"""
        from jiuwen_memory.common.experience import StatsRecord

        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for author, tid in [("d1", "t1"), ("d2", "t2"), ("d3", "t3")]:
            _repro(store, "e1", author, tid)
        # 全有害的统计：validity ≈ 0，远低于基线 0.5
        store.save_stats(EXP_SCOPE, "e1", StatsRecord(
            harmful={"external": 5}, authors=["d1"],
        ))
        store.save_baseline(EXP_SCOPE, {"deploy": 0.5})
        assert TestConditionCheck._checked(kv, entries) is None, "合计有效性低于中位数"


class TestGenerationFlow:
    """生成全链路：abstract 产出草稿与字段（§2.5.4 条目 JSON 例子）。"""

    @staticmethod
    def test_full_flow_produces_draft():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1, "一组通过条件、生成成功 → 一份草稿"
        draft = drafts[0]
        assert draft.scope == EXP_SCOPE, "落点为来源经验所在空间（技能同空间）"
        assert draft.tier == MemoryTier.PROCEDURAL
        assert draft.tags == ["draft"], "manual 方式草稿状态"
        assert sorted(draft.provenance) == ["m-0107", "m-0113"], "来源经验 id"
        metadata = draft.system_metadata
        assert metadata["memory_type"] == "skill"
        assert metadata["skill_name"] == "deploy-config-bootstrap"
        assert metadata["skill_version"] == 1, "无 prev 时版本 1"
        assert metadata["category"] == "deploy"
        assert metadata["capabilities"] == ["bash"]
        assert metadata["source_users"] == 3
        assert metadata["source_trajectories"] == 3
        assert metadata["source_successes"] == 2
        assert metadata["trajectories_read"] == 2, "d1/d2 留有轨迹记录，d3 未留"
        assert metadata["evidence_cutoff"], "证据截止时间非空"
        assert metadata["strategy_version"] == "0"
        assert metadata["author_principal"] == "system:experience"
        assert metadata["author_agent"] == ""
        assert "w" in metadata["induction_batch"], "批次标签形如 2026w41"
        assert draft.content.startswith("---\n"), "SKILL.md 以 YAML 头开头"
        for section in ("## 适用场景", "## 前置条件", "## 步骤", "## 常见失败与处置"):
            assert section in draft.content, f"缺正文节 {section}"

    @staticmethod
    def test_dedup_products_false():
        """SkillAbstractor 声明 dedup_products=False（契约的实现侧）。"""
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            SkillAbstractor,
        )

        assert SkillAbstractor.dedup_products is False

    @staticmethod
    def test_draft_id_deterministic():
        """同组同批证据幂等：条目 id 由经验集合与截止时间算出。"""
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _draft_id,
        )

        first = _draft_id(["b", "a"], "2026-10-08T00:00:00Z")
        assert first == _draft_id(["a", "b"], "2026-10-08T00:00:00Z"), "集合序无关"
        different = _draft_id(["a", "b"], "2026-10-09T00:00:00Z")
        assert first != different, "证据批次不同即不同"

    @staticmethod
    def test_existing_draft_skipped():
        """同组同批证据的草稿已存在时不再产出（幂等，§2.5.3）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        first = abstractor.abstract(entries)
        assert len(first) == 1

        _write_skill(kv, first[0])  # 模拟 evolver 落盘
        second_llm = ScriptedLLM([_skill_json()])
        second = _make_abstractor(
            kv, llm=second_llm, embedder=TagEmbedder()
        ).abstract(entries)
        assert second == [], "同组同批证据不再产出"
        assert second_llm.calls == [], "幂等检查在生成之前，不调模型"

    @staticmethod
    def test_prev_raises_version():
        """prev 存在且条件满足时新草稿版本号加一。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        cutoff = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        prev = _skill_unit(
            "s-1", "deploy-config-bootstrap", version=3,
            provenance=("m-0107",), cutoff=cutoff,
        )
        _write_skill(kv, prev)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1
        assert drafts[0].system_metadata["skill_version"] == 4, "版本 = prev + 1"

    @staticmethod
    def test_empty_model_output_yields_nothing():
        """模型输出空对象（凑不出流程）→ 该组不产出。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM(["{}"])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert abstractor.abstract(entries) == []

    @staticmethod
    def test_invalid_json_retries_then_gives_up():
        """非法 JSON 重试至 max_attempts 后放弃该组，不抛异常。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM(["not-json", "still-not-json", "nope"])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert abstractor.abstract(entries) == []
        assert len(llm.calls) == 3, "尝试 3 次"

    @staticmethod
    def test_no_conditions_met_no_llm_call():
        """没有组通过条件时不调模型（条件检查先于生成）。"""
        kv = InMemoryKVStore()
        entries = [_tip("e1")]  # 无复现事件 → 条件 1 不过
        llm = ScriptedLLM([])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert abstractor.abstract(entries) == []
        assert llm.calls == [], "条件不过不调模型"

    @staticmethod
    def test_max_groups_cap():
        """每轮至多处理 20 组（新证据多的优先）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = []
        for index in range(25):
            # 各占一个类别桶 → 25 组；步骤纯中文（无命令候选）绕开命令覆盖
            entry = _tip(f"e{index}", category=f"cat{index}", cluster="clusterA")
            entries.append(entry)
            _repro(store, f"e{index}", f"d{index}a", f"t{index}a")
            _repro(store, f"e{index}", f"d{index}b", f"t{index}b")
        llm = ScriptedLLM([_skill_json(steps=("复制示例配置", "重启服务"))] * 25)
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), max_groups=5
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 5, "max_groups=5 截断"
        assert len(llm.calls) == 5, "只对处理的组调模型"


class TestTrajectorySelection:
    """轨迹选取（§2.5.3）：至多 5 条、成功的优先、每个用户先各取一条。"""

    @staticmethod
    def test_per_user_first_then_fill():
        """limit 内先保证每个用户各占一条（而非按全局排序截断），再按序补足。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(
            kv, llm=None, embedder=None, trajectories_per_group=2
        )
        # d1 占排序前二（都成功）；纯排序截断会全取 d1，每用户先取一条会带上 d2
        events = [
            ReproductionEvent(author="d1", trajectory_id="t1", outcome=OUTCOME_SUCCESS),
            ReproductionEvent(author="d1", trajectory_id="t2", outcome=OUTCOME_SUCCESS),
            ReproductionEvent(author="d2", trajectory_id="t3", outcome="partial"),
        ]
        picked = _pick_trajectories(abstractor, events)
        assert [(e.author, e.trajectory_id) for e in picked] == [
            ("d1", "t1"), ("d2", "t3"),
        ], "每用户先各取其优先级最高一条（d2 的 partial 不被 d1 的第二条挤掉）"

    @staticmethod
    def test_fill_beyond_per_user():
        """第一遍每用户一条之后，剩余额度按优先级补足。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=None, embedder=None)
        events = [
            ReproductionEvent(author="d1", trajectory_id="t1", outcome="partial"),
            ReproductionEvent(author="d2", trajectory_id="t2", outcome=OUTCOME_SUCCESS),
            ReproductionEvent(author="d1", trajectory_id="t3", outcome=OUTCOME_SUCCESS),
        ]
        picked = _pick_trajectories(abstractor, events)
        assert [(e.author, e.trajectory_id) for e in picked] == [
            ("d2", "t2"), ("d1", "t3"), ("d1", "t1"),
        ], "每用户先各取一条（t2、t3——都是各自最高优先级），再补足 t1"

    @staticmethod
    def test_capped_at_limit():
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=None, embedder=None)
        events = [
            ReproductionEvent(author=f"d{i}", trajectory_id=f"t{i}")
            for i in range(8)
        ]
        picked = _pick_trajectories(abstractor, events)
        assert len(picked) == 5, "至多 5 条"


class TestValidation:
    """第 4 步校验：技能名、脱敏、命令覆盖。"""

    @staticmethod
    def test_unique_skill_name_rules():
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _unique_skill_name,
        )

        assert _unique_skill_name("deploy-config", set()) == "deploy-config"
        assert _unique_skill_name("Deploy_Config", set()) == "deploy-config", "最小规范化"
        assert _unique_skill_name("deploy", {"deploy"}) == "deploy-2", "重名加序号"
        assert _unique_skill_name("deploy", {"deploy", "deploy-2"}) == "deploy-3"
        assert _unique_skill_name("", set()) is None
        assert _unique_skill_name("a" * 65, set()) is None, "超长丢弃"
        assert _unique_skill_name("配置部署", set()) is None, "非法字符丢弃"

    @staticmethod
    def test_desensitization_hit_drops_draft():
        """SKILL.md 命中主体标识 → 丢弃（§2.5.3 脱敏检查）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([
            _skill_json(steps=("给 alice 复制配置", "运行 `python app.py`")),
        ])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), identifiers=["alice"]
        )
        assert abstractor.abstract(entries) == []

    @staticmethod
    def test_command_coverage_ok_with_trajectory():
        """步骤命令能在选出轨迹的工具调用中找到 → 通过。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([
            _skill_json(steps=("复制配置", "运行 `python app.py`")),
        ])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert len(abstractor.abstract(entries)) == 1, "`python app.py` 在轨迹参数中"

    @staticmethod
    def test_command_coverage_fails_on_fabricated_commands():
        """编造的命令过半 → 命令覆盖检查不通过，丢弃。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([
            _skill_json(steps=(
                "运行 `terraform apply`", "运行 `kubectl rollout undo`",
            )),
        ])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert abstractor.abstract(entries) == [], "两条编造命令 0 命中"

    @staticmethod
    def test_command_coverage_falls_back_to_entries():
        """轨迹都读不到时与组内经验的执行动作比对。"""
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _command_coverage_ok,
        )

        entries = [_tip("e1", action="复制 config.example 为 config.yaml")]
        records = [(ReproductionEvent(author="d1", trajectory_id="t1"), None)]
        ok = _command_coverage_ok(
            ["复制 `config.example` 为正式配置"], records, entries
        )
        assert ok, "命令是组内 action 的子串 → 命中"


class TestRenderSkillMd:
    """SKILL.md 渲染（§2.5.4）：YAML 头 + 正文四节。"""

    @staticmethod
    def test_render_shape():
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _GeneratedSkill,
            _render_skill_md,
        )

        skill = _GeneratedSkill(
            name="start-service",
            description="启动缺配置的服务",
            tools=["bash"],
            scenarios="服务报配置缺失时",
            preconditions=["仓库有示例配置"],
            steps=["复制示例配置", "重启服务"],
            failures=["端口占用：改端口"],
        )
        md = _render_skill_md(skill, "start-service")
        assert md.startswith("---\n")
        assert 'name: "start-service"' in md
        assert 'description: "启动缺配置的服务"' in md
        assert "  - \"bash\"" in md
        assert "# start-service" in md
        assert "## 适用场景\n服务报配置缺失时" in md
        assert "## 前置条件\n- 仓库有示例配置" in md
        assert "## 步骤\n1. 复制示例配置\n2. 重启服务" in md
        assert "## 常见失败与处置\n- 端口占用：改端口" in md

    @staticmethod
    def test_yaml_escapes_quotes():
        """description 含引号/换行时 YAML 头仍可被安全加载。"""
        import yaml

        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            _GeneratedSkill,
            _render_skill_md,
        )

        skill = _GeneratedSkill(
            name="x", description='含"引号"与\n换行',
            steps=["一步"], tools=["bash"],
        )
        md = _render_skill_md(skill, "x")
        header = md.split("---")[1]
        parsed = yaml.safe_load(header)
        assert parsed["description"] == '含"引号"与\n换行'
        assert parsed["tools"] == ["bash"]


class TestAutoRelease:
    """auto 发布的附加条件（§2.5.5）：复现更多、读到了轨迹、无危险操作。"""

    @staticmethod
    def test_auto_publishes_trial():
        """auto + 3 用户独立复现 + 读到轨迹 + 无危险操作 → 试用。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), release="auto"
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 1
        assert drafts[0].tags == ["trial"], "auto 满足附加条件 → 试用"

    @staticmethod
    def test_auto_falls_back_to_draft_on_risky_operation():
        """步骤命中危险操作清单 → 不自动发布，退回草稿。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([
            _skill_json(steps=("用 `rm -rf /` 清理", "运行 `python app.py`")),
        ])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), release="auto"
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 1
        assert drafts[0].tags == ["draft"], "危险操作 → 退回草稿等人审"

    @staticmethod
    def test_auto_requires_more_repro():
        """auto 的复现下限（3 用户）高于升为技能的条件（2 用户）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        _repro(store, "e1", "d1", "t1")
        _repro(store, "e1", "d2", "t2")
        _trajectory(kv, "d1", "t1", [("bash", "python app.py")])
        _trajectory(kv, "d2", "t2", [("bash", "python app.py")])
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), release="auto"
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 1
        assert drafts[0].tags == ["draft"], "2 用户 < auto 下限 3 → 草稿"

    @staticmethod
    def test_auto_trajectory_count():
        """轨迹计数的 auto 下限是 5 条独立复现轨迹。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for i in range(1, 6):
            _repro(store, "e1", "d1", f"t{i}")
            _trajectory(kv, "d1", f"t{i}", [("bash", "python app.py")])
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), release="auto", repro_unit="trajectory"
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 1
        assert drafts[0].tags == ["trial"], "5 条独立轨迹 → 试用"

    @staticmethod
    def test_auto_requires_trajectories_read():
        """仅凭经验归纳（轨迹都读不到）不自动发布。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("m-0107"), _tip("m-0113")]
        _repro(store, "m-0107", "d1", "t1")
        _repro(store, "m-0113", "d2", "t2")
        _repro(store, "m-0107", "d3", "t3")
        llm = ScriptedLLM([_skill_json(steps=("复制示例配置", "重启服务"))])
        abstractor = _make_abstractor(
            kv, llm=llm, embedder=TagEmbedder(), release="auto"
        )
        drafts = abstractor.abstract(entries)
        assert len(drafts) == 1
        assert drafts[0].system_metadata["trajectories_read"] == 0
        assert drafts[0].tags == ["draft"], "仅凭经验归纳不自动发布"


class TestAutoReproUnit:
    """repro_unit=auto 的运行时口径（v3 §2.4）：装配期按落点解析到底。

    auto = 写入者自己的空间（fallback 模板渲染得出的形态，如 ``u_{user}``
    渲出的 ``u_alice``）按 trajectory、其余按 user；无判定表时按
    ``default_space`` 兜底。走 abstract() 全链路验证解析结果装进
    ``_Round.repro_unit``（判定处只认 user / trajectory）。同作者 3 条
    独立复现轨迹是两口径的分水岭：trajectory 口径达技能下限（3 条），
    user 口径只算 1 个作者（< 2）。
    """

    OWN_SPACE = "u_alice"
    OWN_SCOPE = Scope(org="acme", space="u_alice")

    @staticmethod
    def _naming():
        """个人空间模板 u_{user}（fallback 类别 personal）的命名规则。"""
        from jiuwen_memory.construction.router import SpaceNaming

        return SpaceNaming(
            templates=(("personal", "u_{user}"), ("project", "p_{project}")),
            fallback_class="personal",
        )

    @staticmethod
    def _abstractor(
        kv, *, space: str, naming, default_space: str = EXP_SPACE, llm: LLM | None = None
    ):
        """经验配置 repro_unit 缺省 auto 的抽象器（manual 发布，聚焦口径解析）。"""
        from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
            SkillAbstractor,
        )

        return SkillAbstractor(
            kv=kv,
            embedder=TagEmbedder(),
            llm=llm if llm is not None else ScriptedLLM([]),
            experience=ExperienceConfig(
                default_space=default_space,
                curated_spaces=(CuratedSpaceConfig(space=space, skill=True),),
            ),
            space_naming=naming,
        )

    @staticmethod
    def test_auto_in_fallback_space_counts_trajectories():
        """auto + 写入者自己的空间（u_alice 命中 u_{user} 形态）→ trajectory。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1", scope=TestAutoReproUnit.OWN_SCOPE)]
        for i in range(1, 4):
            _repro(store, "e1", "d1", f"t{i}", scope=TestAutoReproUnit.OWN_SCOPE)
            _trajectory(kv, "d1", f"t{i}", [("bash", "python app.py")])
        abstractor = TestAutoReproUnit._abstractor(
            kv, space=TestAutoReproUnit.OWN_SPACE, naming=TestAutoReproUnit._naming(),
            llm=ScriptedLLM([_skill_json()]),
        )

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1, "同作者 3 条轨迹在 trajectory 口径达下限 → 归纳出草稿"

    @staticmethod
    def test_auto_in_shared_space_counts_users():
        """auto + 共享空间（不命中 fallback 模板）→ user：同作者只算 1 个用户。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1")]
        for i in range(1, 4):
            _repro(store, "e1", "d1", f"t{i}")
            _trajectory(kv, "d1", f"t{i}", [("bash", "python app.py")])
        abstractor = TestAutoReproUnit._abstractor(
            kv, space=EXP_SPACE, naming=TestAutoReproUnit._naming()
        )

        drafts = abstractor.abstract(entries)

        assert drafts == [], "同作者 3 条轨迹在 user 口径只算 1 个作者 < 下限 2"

    @staticmethod
    def test_auto_without_naming_owns_by_default_space():
        """无判定表时按 default_space 兜底：own → 写入者自己的空间按 trajectory。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = [_tip("e1", scope=TestAutoReproUnit.OWN_SCOPE)]
        for i in range(1, 4):
            _repro(store, "e1", "d1", f"t{i}", scope=TestAutoReproUnit.OWN_SCOPE)
            _trajectory(kv, "d1", f"t{i}", [("bash", "python app.py")])
        abstractor = TestAutoReproUnit._abstractor(
            kv, space=TestAutoReproUnit.OWN_SPACE, naming=None, default_space="own",
            llm=ScriptedLLM([_skill_json()]),
        )

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1, "default_space=own 兜底 → trajectory 口径，3 条轨迹达标"


class TestRunSummaryAppend:
    """运行摘要补写（§2.5.2）：归纳统计补进最近一条 run-summary。"""

    @staticmethod
    def test_appends_skill_induction():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        summary = _write_run_summary(kv, json.dumps({"steps": ["聚合"]}))
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1
        raw = kv.get(EXP_SCOPE, memory_key(summary.id))
        payload = json.loads(loads(raw).content)
        assert payload["steps"] == ["聚合"], "原有字段不动"
        assert payload["skill_induction"] == {
            "groups": 1, "drafts": 1, "experience_only": 0,
        }, "组数、草稿数与其中仅凭经验归纳的份数"

    @staticmethod
    def test_no_run_summary_no_error():
        """没有 run-summary 条目时跳过补写，不抛异常。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert len(abstractor.abstract(entries)) == 1

    @staticmethod
    def test_experience_only_counted():
        """轨迹全读不到时草稿计入 experience_only。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        summary = _write_run_summary(kv)
        entries = [_tip("m-0107"), _tip("m-0113")]
        _repro(store, "m-0107", "d1", "t1")
        _repro(store, "m-0113", "d2", "t2")
        _repro(store, "m-0107", "d3", "t3")
        llm = ScriptedLLM([_skill_json(steps=("复制示例配置", "重启服务"))])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1
        payload = json.loads(loads(kv.get(EXP_SCOPE, memory_key(summary.id))).content)
        assert payload["skill_induction"] == {
            "groups": 1, "drafts": 1, "experience_only": 1,
        }


class TestDegradation:
    """配置降级（§2.5.3）：不炸轮次，返回空。"""

    @staticmethod
    def test_no_llm_returns_empty():
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, embedder=TagEmbedder())
        assert abstractor.abstract([_tip("e1")]) == []

    @staticmethod
    def test_no_embedder_returns_empty():
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=ScriptedLLM([]))
        assert abstractor.abstract([_tip("e1")]) == []

    @staticmethod
    def test_skill_disabled_space_returns_empty():
        """经验落点空间未声明 ``skill: true``（技能沉淀未开启）→ 返回空。"""
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(
            kv, llm=ScriptedLLM([]), embedder=TagEmbedder(),
            experience=ExperienceConfig(default_space=EXP_SPACE),
        )
        assert abstractor.abstract([_tip("e1")]) == []

    @staticmethod
    def test_empty_input_returns_empty():
        kv = InMemoryKVStore()
        abstractor = _make_abstractor(kv, llm=ScriptedLLM([]), embedder=TagEmbedder())
        assert abstractor.abstract([]) == []


class TestSkillNameValidation:
    """技能名规则的生成侧：名字不合规丢弃整组草稿。"""

    @staticmethod
    def test_invalid_model_name_drops_draft():
        """模型给的名字规范化后仍不合规 → 丢弃。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        llm = ScriptedLLM([_skill_json(name="配置 部署!")])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())
        assert abstractor.abstract(entries) == []

    @staticmethod
    def test_duplicate_name_gets_suffix():
        """与技能空间已有技能重名 → 加序号（不丢弃）。"""
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        entries = _seed_qualifying_group(kv, store)
        _write_skill(kv, _skill_unit("s-old", "deploy-config-bootstrap"))
        llm = ScriptedLLM([_skill_json()])
        abstractor = _make_abstractor(kv, llm=llm, embedder=TagEmbedder())

        drafts = abstractor.abstract(entries)

        assert len(drafts) == 1
        name = drafts[0].system_metadata["skill_name"]
        assert name == "deploy-config-bootstrap-2", "重名加序号"
