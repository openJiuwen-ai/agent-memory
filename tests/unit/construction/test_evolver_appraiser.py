"""Evolver 的评价算子接线与去重结论范围守卫测试（mem2.0 §2.2）。

OrchestratingEvolver：``appraise`` 准入（``proceed`` 为否短路、异常照常冒泡）→
评价结论经 ``ExtractContext.appraisal`` 传给抽取算子（非 infer 写入 context 为
``None`` 时补空壳挂上）→ 流程结束 ``finalize``（流程出错以 ``None`` 调、收尾自身
出错只记日志）。未配置评价算子时两处都跳过，其他 profile 行为不变。

DynamicEvolver：``dedup_decisions`` 按抽取策略名限定允许的去重结论（范围外一律
按新增处理）、NOOP 把命中的已有条目 id 记入 ``EvolveResult.duplicate_ids``
（评价算子 finalize 记复现的数据源）。
"""

from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    DedupDecision,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
)
from jiuwen_memory.construction.appraiser import Appraisal, Appraiser
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.evolver import EvolveMode, EvolveResult
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
)
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.prompt_registry import PromptRegistry
from jiuwen_memory.construction.prompt_strategy import EXTRACTION_STRATEGY_KEY
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import IndexWriteMode
from tests.conftest import make_storage

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 轻量替身
# ---------------------------------------------------------------------------


class _ScriptedLLM(LLM):
    """按序返回预排响应的 LLM；判定路径不预期调用时响应耗尽返回空串。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.messages: list[list] = []

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.LLM

    @staticmethod
    def health() -> None:
        return None

    def chat(self, messages, **options):
        self.messages.append(messages)
        return self.responses.pop(0) if self.responses else ""


class _RecordingAppraiser(Appraiser):
    """记录 appraise / finalize 调用；可注入异常验证收尾语义。"""

    def __init__(
        self,
        appraisal: Appraisal | None = None,
        *,
        appraise_error: Exception | None = None,
        finalize_error: Exception | None = None,
    ) -> None:
        self.appraise_calls: list[list[MemoryUnit]] = []
        self.finalize_calls: list[tuple[list[MemoryUnit], Appraisal, EvolveResult | None]] = []
        self._appraisal = appraisal
        self._appraise_error = appraise_error
        self._finalize_error = finalize_error

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.APPRAISER

    @staticmethod
    def health() -> None:
        return None

    def appraise(self, units: list[MemoryUnit]) -> Appraisal:
        self.appraise_calls.append(list(units))
        if self._appraise_error is not None:
            raise self._appraise_error
        return self._appraisal

    def finalize(self, units, appraisal, result) -> None:
        self.finalize_calls.append((list(units), appraisal, result))
        if self._finalize_error is not None:
            raise self._finalize_error


class _RecordingExtractor(Extractor):
    """记录收到的抽取上下文；可注入异常验证流程出错时的收尾。"""

    def __init__(
        self,
        output: list[MemoryUnit] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.contexts: list[ExtractContext | None] = []
        self._output = list(output or [])
        self._error = error

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.EXTRACTOR

    @staticmethod
    def health() -> None:
        return None

    def extract(self, units, *, context=None) -> list[MemoryUnit]:
        self.contexts.append(context)
        if self._error is not None:
            raise self._error
        return list(self._output)


class _Dedup:
    """去重替身：recall 恒返回预置 hits（(unit, score) 列表）。"""

    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])

    def recall(self, candidate):
        return list(self.hits)


class _Index:
    """IndexBuilder 替身：记录落盘并交付 Storage（记忆写入的唯一入口）。"""

    def __init__(self, storage=None) -> None:
        self.built: list[MemoryUnit] = []
        self._storage = storage

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.built.extend(units)
        if self._storage is not None:
            for unit in units:
                self._storage.add(unit.scope, [unit])

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        if self._storage is not None:
            for unit in units:
                self._storage.update(unit.scope, [unit])

    @staticmethod
    def remove(unit_ids):
        pass

    @staticmethod
    def rebuild():
        pass


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _unit(unit_id: str, content: str, metadata: dict | None = None) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        system_metadata=dict(metadata or {}),
    )


def _candidate(unit_id: str = "candidate-1", content: str = "新事实") -> MemoryUnit:
    """抽取算子的产出候选。"""
    return _unit(unit_id, content)


def _make_orchestrating(
    *,
    extractor: _RecordingExtractor | None = None,
    appraiser: _RecordingAppraiser | None = None,
    dedup: _Dedup | None = None,
) -> tuple[OrchestratingEvolver, InMemoryKVStore, _Index]:
    kv = InMemoryKVStore()
    storage = make_storage(kv=kv, graph=InMemoryGraphStore())
    index = _Index(storage.domain_store())
    evolver = OrchestratingEvolver(
        extractor=extractor or _RecordingExtractor(),
        abstractor=object(),  # EXTRACT 路径不触发 abstractor
        associator=object(),  # EXTRACT 路径不触发 associator
        index_builder=index,
        storage=storage,
        message_store=storage.kv(),
        dedup=dedup or _Dedup([]),
        llm=_ScriptedLLM(),
        layer_annotator=None,
        appraiser=appraiser,
    )
    return evolver, kv, index


def _make_dynamic(
    *,
    extractor: _RecordingExtractor | None = None,
    appraiser: _RecordingAppraiser | None = None,
    dedup: _Dedup | None = None,
    llm: _ScriptedLLM | None = None,
    dedup_decisions: dict[str, set[DedupDecision]] | None = None,
    prompts: dict | None = None,
) -> tuple[DynamicEvolver, InMemoryKVStore, _Index]:
    kv = InMemoryKVStore()
    storage = make_storage(kv=kv, graph=InMemoryGraphStore())
    index = _Index(storage.domain_store())
    evolver = DynamicEvolver(
        extractor=extractor or _RecordingExtractor(),
        abstractor=object(),
        associator=object(),
        index_builder=index,
        storage=storage,
        message_store=storage.kv(),
        dedup=dedup or _Dedup([]),
        llm=llm or _ScriptedLLM(),
        layer_annotator=None,
        appraiser=appraiser,
        prompt_registry=PromptRegistry.from_dict(prompts or {}),
        dedup_decisions=dedup_decisions,
    )
    return evolver, kv, index


# ---------------------------------------------------------------------------
# OrchestratingEvolver：评价算子接线
# ---------------------------------------------------------------------------


class TestOrchestratingAppraiserWiring:
    @staticmethod
    def test_no_appraiser_keeps_behavior_unchanged():
        """未配置评价算子：照常抽取，不收集评价结论（其他 profile 行为不变）。"""
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result.created_ids == ["candidate-1"], "未配置评价算子照常落盘"
        assert extractor.contexts == [None], "非 infer 且无评价算子时上下文为 None"

    @staticmethod
    def test_proceed_false_short_circuits():
        """准入不通过：不抽取、不收尾，返回空结果（轨迹状态由 appraise 自己置好）。"""
        appraiser = _RecordingAppraiser(appraisal=Appraisal(proceed=False))
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result == EvolveResult(), "准入不通过返回空 EvolveResult"
        assert len(appraiser.appraise_calls) == 1, "评价算子只被调一次"
        assert extractor.contexts == [], "准入不通过不进抽取"
        assert appraiser.finalize_calls == [], "准入不通过不收尾"

    @staticmethod
    def test_appraisal_attached_to_fresh_context():
        """非 infer 写入：context 为 None 时补空壳挂上评价结论。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)

        evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        context = extractor.contexts[0]
        assert isinstance(context, ExtractContext), "非 infer 写入补空壳上下文"
        assert context.appraisal is appraisal, "评价结论挂进抽取上下文"

    @staticmethod
    def test_appraisal_attached_to_collected_context():
        """infer=true 写入：评价结论挂到已收集的上下文（不覆盖参考项）。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(output=[_candidate()])
        related = _unit("related-1", "已有相关事实")
        evolver, _, _ = _make_orchestrating(
            extractor=extractor,
            appraiser=appraiser,
            dedup=_Dedup([(related, 0.9)]),
        )
        source = _unit("req-1", "原始内容", {"infer": "true"})

        evolver.evolve([source], EvolveMode.EXTRACT)

        context = extractor.contexts[0]
        assert context is not None and context.appraisal is appraisal, (
            "评价结论挂进已收集的上下文"
        )
        assert [unit.id for unit in context.related_memories] == ["related-1"], (
            "收集的相关记忆参考项不受影响"
        )

    @staticmethod
    def test_finalize_receives_result():
        """流程正常结束：finalize 收到落盘结果。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)
        units = [_unit("req-1", "原始内容")]

        result = evolver.evolve(units, EvolveMode.EXTRACT)

        assert result.created_ids == ["candidate-1"], "前置：候选照常落盘"
        assert len(appraiser.finalize_calls) == 1, "流程结束收尾一次"
        finalized_units, finalized_appraisal, finalized_result = (
            appraiser.finalize_calls[0]
        )
        assert finalized_units == units, "收尾拿到本轮写入的 units"
        assert finalized_appraisal is appraisal, "收尾拿到同一份评价结论"
        assert finalized_result is result, "收尾拿到落盘结果本身"

    @staticmethod
    def test_extractor_error_finalizes_with_none_and_reraises():
        """流程抛异常：finalize 以 None 调（评价算子据此置轨迹失败），异常照常冒泡。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(error=RuntimeError("boom"))
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)

        with pytest.raises(RuntimeError, match="boom"):
            evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert len(appraiser.finalize_calls) == 1, "流程出错也要收尾一次"
        assert appraiser.finalize_calls[0][2] is None, "出错的收尾以 None 为结果"

    @staticmethod
    def test_finalize_error_is_swallowed():
        """收尾自身出错只记日志：条目可能已落盘，抛出会把成功写入误报为失败。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(
            appraisal=appraisal, finalize_error=ValueError("finalize boom")
        )
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result.created_ids == ["candidate-1"], "收尾出错不影响落盘结果返回"

    @staticmethod
    def test_appraise_error_propagates_without_finalize():
        """评价自身抛异常：照常冒泡，不进抽取也不收尾（状态由实现自己置失败）。"""
        appraiser = _RecordingAppraiser(appraise_error=RuntimeError("appraise boom"))
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_orchestrating(extractor=extractor, appraiser=appraiser)

        with pytest.raises(RuntimeError, match="appraise boom"):
            evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert extractor.contexts == [], "评价失败不进抽取"
        assert appraiser.finalize_calls == [], "评价失败不收尾"


# ---------------------------------------------------------------------------
# DynamicEvolver：评价算子接线（覆盖版 _evolve_extract）
# ---------------------------------------------------------------------------


class TestDynamicAppraiserWiring:
    @staticmethod
    def test_proceed_false_short_circuits():
        """准入不通过：动态四步整段跳过，不抽取、不收尾。"""
        appraiser = _RecordingAppraiser(appraisal=Appraisal(proceed=False))
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_dynamic(extractor=extractor, appraiser=appraiser)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result == EvolveResult(), "准入不通过返回空 EvolveResult"
        assert extractor.contexts == [], "准入不通过不进抽取"
        assert appraiser.finalize_calls == [], "准入不通过不收尾"

    @staticmethod
    def test_appraisal_attached_to_fresh_context():
        """评价结论经上下文传给抽取算子（与父类接法相同）。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_dynamic(extractor=extractor, appraiser=appraiser)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        context = extractor.contexts[0]
        assert context is not None and context.appraisal is appraisal, (
            "评价结论挂进抽取上下文"
        )
        assert result.created_ids == ["candidate-1"], "无召回命中按新增落盘"

    @staticmethod
    def test_extractor_error_finalizes_with_none_and_reraises():
        """流程抛异常：finalize 以 None 调，异常照常冒泡（与父类接法相同）。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(error=RuntimeError("boom"))
        evolver, _, _ = _make_dynamic(extractor=extractor, appraiser=appraiser)

        with pytest.raises(RuntimeError, match="boom"):
            evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert appraiser.finalize_calls[0][2] is None, "出错的收尾以 None 为结果"

    @staticmethod
    def test_finalize_receives_result():
        """流程正常结束：finalize 收到落盘结果。"""
        appraisal = Appraisal(proceed=True)
        appraiser = _RecordingAppraiser(appraisal=appraisal)
        extractor = _RecordingExtractor(output=[_candidate()])
        evolver, _, _ = _make_dynamic(extractor=extractor, appraiser=appraiser)

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert appraiser.finalize_calls[0][2] is result, "收尾拿到落盘结果本身"


# ---------------------------------------------------------------------------
# DynamicEvolver：dedup_decisions 范围守卫
# ---------------------------------------------------------------------------


class TestDedupDecisionScopes:
    @staticmethod
    def test_dedup_decisions_for_three_states():
        """未配置返回 None（四种都允许）；候选无策略名返回 None；声明命中返回集合。"""
        evolver, _, _ = _make_dynamic(
            dedup_decisions={"experience_tip": {DedupDecision.ADD, DedupDecision.NOOP}}
        )
        no_config = _make_dynamic()[0]

        declared = _unit("c1", "c", {EXTRACTION_STRATEGY_KEY: "experience_tip"})
        undeclared = _unit("c2", "c", {EXTRACTION_STRATEGY_KEY: "experience_fact"})
        no_strategy = _unit("c3", "c")

        assert getattr(no_config, "_dedup_decisions_for")(declared) is None, (
            "未配置范围时不限定结论"
        )
        for ev in (evolver, no_config):
            assert getattr(ev, "_dedup_decisions_for")(no_strategy) is None, (
                "候选无策略名不限定结论"
            )
        assert getattr(evolver, "_dedup_decisions_for")(declared) == {
            DedupDecision.ADD, DedupDecision.NOOP,
        }, "声明的策略返回允许结论集合"
        assert getattr(evolver, "_dedup_decisions_for")(undeclared) is None, (
            "未声明的策略不限定结论"
        )

    @staticmethod
    def test_llm_judge_out_of_scope_falls_back_to_add():
        """范围外结论（update）一律按新增处理：existing 置 None、score 归零。"""
        evolver, _, _ = _make_dynamic(
            dedup_decisions={"experience_tip": {DedupDecision.ADD, DedupDecision.NOOP}}
        )
        candidate = _unit("c1", "新事实", {EXTRACTION_STRATEGY_KEY: "experience_tip"})
        existing = _unit("existing", "旧事实")
        llm = getattr(evolver, "_llm")
        llm.responses = [
            json.dumps({"decision": "update", "existing_id": "existing", "reason": "x"})
        ]

        decision, hit, score = getattr(evolver, "_llm_judge")(
            candidate, [(existing, 0.8)], "prompt"
        )

        assert decision == DedupDecision.ADD, "范围外的修改型结论按新增处理"
        assert hit is None, "按新增处理不携带已有条目"
        assert score == pytest.approx(0.0), "按新增处理相似度归零"

    @staticmethod
    def test_llm_judge_in_scope_decision_kept():
        """范围内的结论（noop）照常返回已有条目与相似度。"""
        evolver, _, _ = _make_dynamic(
            dedup_decisions={"experience_tip": {DedupDecision.ADD, DedupDecision.NOOP}}
        )
        candidate = _unit("c1", "新事实", {EXTRACTION_STRATEGY_KEY: "experience_tip"})
        existing = _unit("existing", "旧事实")
        llm = getattr(evolver, "_llm")
        llm.responses = [
            json.dumps({"decision": "noop", "existing_id": "existing", "reason": "x"})
        ]

        decision, hit, score = getattr(evolver, "_llm_judge")(
            candidate, [(existing, 0.8)], "prompt"
        )

        assert decision == DedupDecision.NOOP, "范围内的重复结论照常返回"
        assert hit is existing and score == pytest.approx(0.8), (
            "重复结论携带命中的已有条目"
        )

    @staticmethod
    def test_judge_update_without_existing_id_falls_back_to_add():
        """修改型决策未引用有效 existing_id：判定失败降级为新增。"""
        evolver, _, _ = _make_dynamic(
            dedup_decisions={"episodic": {
                DedupDecision.ADD, DedupDecision.UPDATE, DedupDecision.NOOP,
            }}
        )
        candidate = _unit(
            "c1",
            "新事实",
            {EXTRACTION_STRATEGY_KEY: "episodic"},
        )
        existing = _unit("existing", "旧事实")
        llm = getattr(evolver, "_llm")
        llm.responses = [json.dumps({"decision": "update", "reason": "x"})]

        decision, hit, score = getattr(evolver, "_judge")(candidate, [(existing, 0.8)])

        assert decision == DedupDecision.ADD, "缺 existing_id 的修改型决策降级为新增"
        assert hit is existing, "降级时保留召回信息供日志"
        assert score == pytest.approx(0.8), "降级时保留召回相似度"

    @staticmethod
    def test_evolve_out_of_scope_decision_falls_back_to_add():
        """全流程：策略只允许新增/重复，模型判 supersede → 按新增落盘。"""
        existing = _unit("existing", "旧事实")
        llm = _ScriptedLLM([
            json.dumps({"decision": "supersede", "existing_id": "existing", "reason": "x"})
        ])
        # extractor 产出的候选带策略名与巩固 prompt，才能走到 LLM 判定。
        candidate = _candidate()
        candidate.system_metadata[EXTRACTION_STRATEGY_KEY] = "episodic"
        candidate.system_metadata["_consolidation_prompt_episodic"] = "episodic"
        evolver, _, _ = _make_dynamic(
            extractor=_RecordingExtractor(output=[candidate]),
            dedup=_Dedup([(existing, 0.8)]),
            llm=llm,
            dedup_decisions={"episodic": {DedupDecision.ADD, DedupDecision.NOOP}},
            prompts={"consolidate": {"episodic": "事件变化时替换旧记忆"}},
        )

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result.created_ids == ["candidate-1"], "范围外结论按新增落盘"
        assert result.superseded_ids == [], "不执行被拒的取代"
        assert llm.messages, "前置：确实经过了 LLM 判定"

    @staticmethod
    def test_apply_decision_noop_records_duplicate_ids():
        """NOOP 决策：命中的已有条目 id 记入 duplicate_ids（复现记录的数据源）。"""
        evolver, _, _ = _make_dynamic()
        existing = _unit("existing", "旧事实")
        result = EvolveResult()

        count = getattr(evolver, "_apply_decision")(
            _candidate(), DedupDecision.NOOP, existing, 0.95, result
        )

        assert count == 1, "NOOP 计为一次跳过"
        assert result.duplicate_ids == ["existing"], "复现目标记入 duplicate_ids"
        assert result.created_ids == [], "重复候选不落盘"

    @staticmethod
    def test_apply_decision_noop_without_existing_records_nothing():
        """NOOP 无有效已有条目：不填 duplicate_ids（不用相似度最高者代替）。"""
        evolver, _, _ = _make_dynamic()
        result = EvolveResult()

        getattr(evolver, "_apply_decision")(
            _candidate(), DedupDecision.NOOP, None, 0.95, result
        )

        assert result.duplicate_ids == [], "无有效已有条目不误记复现"

    @staticmethod
    def test_evolve_noop_flow_fills_duplicate_ids():
        """全流程：高相似同内容 direct_noop → duplicate_ids 记复现目标。"""
        existing = _unit("existing", "完全相同的记忆")
        evolver, _, index = _make_dynamic(
            extractor=_RecordingExtractor(output=[_candidate(content="完全相同的记忆")]),
            dedup=_Dedup([(existing, 0.95)]),
        )

        result = evolver.evolve([_unit("req-1", "原始内容")], EvolveMode.EXTRACT)

        assert result.created_ids == [], "重复候选不落盘"
        assert result.duplicate_ids == ["existing"], "复现目标记入 duplicate_ids"
        assert index.built == [], "IndexBuilder 未收到重复候选"
