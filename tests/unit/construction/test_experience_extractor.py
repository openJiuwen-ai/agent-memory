"""ExperienceExtractor：轨迹沉淀八步中的第 6 步（mem2.0 §2.2）。

重点覆盖：只有评价结论在上下文里才提炼（未配评价算子的 profile 不产出）、
三段式调用链（提炼 → 条件修正 → 分类）的条件执行、三道闸（脱敏 / 类别写入
上限 / 可执行标记）的丢弃语义、候选 MemoryUnit 的元数据形状（经验条目不属于
任何用户、作者标记固定为系统主体）。
"""

from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.experience import OUTCOME_FAILURE, OUTCOME_SUCCESS, UtilityStore, now_iso
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory import MEMORY_CLASS_KEY, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps as dump_unit
from jiuwen_memory.construction.appraiser import Appraisal
from jiuwen_memory.construction.base import ExtractContext
from jiuwen_memory.construction.experience import (
    ExecutionTrajectoryRecord,
    ExperienceAppraisal,
    ExperienceConfig,
    VerdictRecord,
)
from jiuwen_memory.construction.extractor_impl.experience_extractor import (
    AUTHOR_PRINCIPAL_SYSTEM,
    CATEGORY_WRITE_LIMIT,
    DEFAULT_CATEGORY,
    STRATEGY_FACT,
    STRATEGY_TIP,
    STRATEGY_VERSION_DEFAULT,
    ExperienceExtractor,
    is_executable_action,
)
from jiuwen_memory.construction.prompt_strategy import EXTRACTION_STRATEGY_KEY
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.unit.construction.fixtures import ScriptedLLM

pytestmark = pytest.mark.unit

EXPERIENCE_SPACE = "shared-experience"
SCOPE = Scope(org="acme", user="u1")
EXP_SCOPE = Scope(org="acme", space=EXPERIENCE_SPACE)


def _source_unit(*, user: str = "u1") -> MemoryUnit:
    return MemoryUnit(
        id="req-1",
        scope=Scope(org="acme", user=user),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content="{}", source=Modality.TEXT)],
        temporal=Temporal(t_message=None),
        system_metadata={"memory_type": "experience", "pipeline": "default"},
    )


def _record(
    *,
    messages: list[dict] | None = None,
    retrieved: list | None = None,
    user: Scope | None = None,
) -> ExecutionTrajectoryRecord:
    return ExecutionTrajectoryRecord(
        trajectory_id="t1",
        user=user if user is not None else SCOPE,
        producer=Scope(org="acme", agent="agent-core"),
        messages=messages or [{"role": "user", "content": "run the deploy"}],
        retrieved=retrieved,
        tool_call_count=3,
        started_at=now_iso(),
    )


def _verdict(outcome: str = OUTCOME_SUCCESS) -> VerdictRecord:
    return VerdictRecord(outcome=outcome, judged_at=now_iso())


def _context(
    *,
    worth: bool = True,
    outcome: str = OUTCOME_SUCCESS,
    retrieved: list | None = None,
    attribution: dict[str, str] | None = None,
    messages: list[dict] | None = None,
) -> ExtractContext:
    return ExtractContext(
        appraisal=Appraisal(
            proceed=True,
            data=ExperienceAppraisal(
                record=_record(messages=messages, retrieved=retrieved),
                verdict=_verdict(outcome),
                attribution=attribution or {},
                worth_extracting=worth,
            ),
        )
    )


def _seed_entry(kv, entry_id: str, content: str) -> None:
    unit = MemoryUnit(
        id=entry_id,
        scope=EXP_SCOPE,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
    )
    kv.insert(EXP_SCOPE, memory_key(entry_id), dump_unit(unit))


def _extractor(kv, llm, **overrides) -> ExperienceExtractor:
    params = dict(
        llm=llm,
        kv=kv,
        # 缺省落点配为空间名：候选与取用条目都落 EXP_SCOPE（own 形态另测）。
        experience=ExperienceConfig(default_space=EXPERIENCE_SPACE),
        retry_backoff_ms=0,
    )
    params.update(overrides)
    return ExperienceExtractor(**params)


def _extract_payload(facts=None, tips=None) -> str:
    return json.dumps({"facts": facts or [], "tips": tips or []})


def _classify_payload(categories: dict) -> str:
    return json.dumps({"categories": categories})


# ====================================================================== #
# 准入与装配
# ====================================================================== #


class TestGating:
    @staticmethod
    def test_no_context_returns_empty():
        extractor = _extractor(InMemoryKVStore(), ScriptedLLM())
        assert extractor.extract([_source_unit()]) == [], "无上下文不提炼"

    @staticmethod
    def test_no_appraisal_returns_empty():
        extractor = _extractor(InMemoryKVStore(), ScriptedLLM())
        assert extractor.extract([_source_unit()], context=ExtractContext()) == [], (
            "上下文无评价结论不提炼"
        )

    @staticmethod
    def test_non_experience_appraisal_returns_empty():
        extractor = _extractor(InMemoryKVStore(), ScriptedLLM())
        context = ExtractContext(appraisal=Appraisal(proceed=True, data=None))
        assert extractor.extract([_source_unit()], context=context) == [], (
            "评价结论不是经验的实现时不提炼"
        )

    @staticmethod
    def test_not_worth_extracting_returns_empty():
        llm = ScriptedLLM()
        extractor = _extractor(InMemoryKVStore(), llm)
        result = extractor.extract(
            [_source_unit()], context=_context(worth=False)
        )
        assert result == [], "不值得提炼不产出候选"
        assert llm.calls == [], "不值得提炼不调模型"


# ====================================================================== #
# 提炼、修正与分类
# ====================================================================== #


class TestDistillFlow:
    @staticmethod
    def test_facts_and_tips_built_into_units():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(
                facts=["config/app.yaml is required by python -m app"],
                tips=[{
                    "condition": "python -m app reports config/app.yaml missing",
                    "capability": "bash",
                    "action": "cp config/app.example.yaml config/app.yaml",
                }],
            ),
            _classify_payload({
                "e0": "software-engineering-devops",
                "t0": "software-engineering-devops",
            }),
        ])
        units = _extractor(kv, llm).extract(
            [_source_unit()], context=_context()
        )
        assert len(units) == 2, "一条 fact 一条 tip 各成一个候选"
        fact, tip = units
        assert fact.tier is MemoryTier.SEMANTIC, "环境事实归 SEMANTIC"
        assert tip.tier is MemoryTier.PROCEDURAL, "条件动作归 PROCEDURAL"
        assert fact.content == "config/app.yaml is required by python -m app", (
            "fact 正文原句"
        )
        expected = (
            "python -m app reports config/app.yaml missing"
            "时，用 bash cp config/app.example.yaml config/app.yaml"
        )
        assert tip.content == expected, "tip 正文按模板渲染"
        for unit in units:
            assert unit.scope == EXP_SCOPE, "候选落点为经验空间（不属于任何用户）"
            meta = unit.system_metadata
            assert meta[MEMORY_CLASS_KEY] == "experience", "经验类别标记"
            assert meta["category"] == "software-engineering-devops", "分类回填"
            assert meta["strategy_version"] == STRATEGY_VERSION_DEFAULT, "策略版本"
            assert meta["author_principal"] == AUTHOR_PRINCIPAL_SYSTEM, (
                "作者标记固定为系统主体"
            )
            assert meta["memory_type"] == "experience", "memory_type 沿用"
        assert fact.system_metadata[EXTRACTION_STRATEGY_KEY] == STRATEGY_FACT, (
            "fact 的抽取策略名"
        )
        tip_meta = tip.system_metadata
        assert tip_meta[EXTRACTION_STRATEGY_KEY] == STRATEGY_TIP, "tip 的抽取策略名"
        assert tip_meta["condition"] == (
            "python -m app reports config/app.yaml missing"
        ), "tip 三段存元数据"
        assert tip_meta["capability"] == "bash", "tip 三段存元数据"
        assert tip_meta["action"] == "cp config/app.example.yaml config/app.yaml", (
            "tip 三段存元数据"
        )
        assert tip_meta["executable"] is True, "含命令的动作判可执行"
        assert units[0].provenance == ["req-1"], "血缘回指轨迹请求 unit"

    @staticmethod
    def test_default_space_own_lands_on_write_scope():
        """``default_space: own``：候选落写入落点，即轨迹请求 unit 的 scope 所在空间。"""
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(facts=["config/app.yaml is required"]),
            _classify_payload({"e0": "other"}),
        ])
        extractor = _extractor(
            kv, llm, experience=ExperienceConfig(default_space="own")
        )
        source = MemoryUnit(
            id="req-own",
            scope=Scope(org="acme", space="u_alice"),
            tier=MemoryTier.EPISODIC,
            segments=[Segment(content="{}", source=Modality.TEXT)],
            temporal=Temporal(t_message=None),
            system_metadata={"memory_type": "experience", "pipeline": "default"},
        )
        units = extractor.extract([source], context=_context())
        assert units, "有候选"
        for unit in units:
            assert unit.scope == Scope(org="acme", space="u_alice"), (
                "own 时落点为写入者自己的空间（写入落点的空间维）"
            )

    @staticmethod
    def test_empty_drafts_skip_classify():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([_extract_payload()])
        units = _extractor(kv, llm).extract(
            [_source_unit()], context=_context()
        )
        assert units == [], "无候选不产出"
        assert len(llm.calls) == 1, "无候选时不调分类模型"

    @staticmethod
    def test_unknown_or_missing_category_falls_back():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(facts=["fact-a", "fact-b"]),
            _classify_payload({"e0": "not-a-category", "e1": "data-analytics-and-visualization"}),
        ])
        units = _extractor(kv, llm).extract(
            [_source_unit()], context=_context()
        )
        assert units[0].system_metadata["category"] == DEFAULT_CATEGORY, (
            "未知类别归 other"
        )
        assert units[1].system_metadata["category"] == "data-analytics-and-visualization", (
            "表内类别回填"
        )


class TestRevise:
    @staticmethod
    def test_failure_with_ineffective_entry_revises():
        kv = InMemoryKVStore()
        _seed_entry(kv, "e1", "tip: always delete the config before restart")
        llm = ScriptedLLM([
            _extract_payload(),
            json.dumps({"tips": [{
                "condition": "restart needs the config present",
                "capability": "bash",
                "action": "keep config/app.yaml and run python -m app",
            }]}),
            _classify_payload({"t0": "other"}),
        ])
        units = _extractor(kv, llm).extract(
            [_source_unit()],
            context=_context(
                outcome=OUTCOME_FAILURE,
                retrieved=[{"id": "e1", "ref": "0.n"}],
                attribution={"e1": "harmful"},
            ),
        )
        assert len(units) == 1, "修正产出的 tip 成为一个候选"
        assert "keep config/app.yaml" in units[0].content, "修正内容进入候选"
        assert len(llm.calls) == 3, "提炼、修正、分类各一次调用"

    @staticmethod
    def test_revise_keeps_only_first_tip():
        kv = InMemoryKVStore()
        _seed_entry(kv, "e1", "tip: always delete the config")
        llm = ScriptedLLM([
            _extract_payload(),
            json.dumps({"tips": [
                {"condition": "c1", "capability": "bash", "action": "action-one"},
                {"condition": "c2", "capability": "bash", "action": "action-two"},
            ]}),
            _classify_payload({"t0": "other"}),
        ])
        units = _extractor(kv, llm).extract(
            [_source_unit()],
            context=_context(
                outcome=OUTCOME_FAILURE,
                retrieved=[{"id": "e1", "ref": "0.n"}],
                attribution={"e1": "harmful"},
            ),
        )
        assert len(units) == 1 and "action-one" in units[0].content, (
            "修正至多产出一条（多条时取第一条）"
        )

    @staticmethod
    def test_success_skips_revise():
        kv = InMemoryKVStore()
        _seed_entry(kv, "e1", "tip: something")
        llm = ScriptedLLM([
            _extract_payload(facts=["fact-a"]),
            _classify_payload({"e0": "other"}),
        ])
        _extractor(kv, llm).extract(
            [_source_unit()],
            context=_context(
                outcome=OUTCOME_SUCCESS,
                retrieved=[{"id": "e1", "ref": "0.n"}],
                attribution={"e1": "helpful"},
            ),
        )
        assert len(llm.calls) == 2, "任务成功不做修正（提炼 + 分类）"

    @staticmethod
    def test_no_retrieved_skips_revise():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(facts=["fact-a"]),
            _classify_payload({"e0": "other"}),
        ])
        _extractor(kv, llm).extract(
            [_source_unit()], context=_context(outcome=OUTCOME_FAILURE)
        )
        assert len(llm.calls) == 2, "未取用条目无从针对，不做修正"

    @staticmethod
    def test_missing_entries_skip_revise():
        """取用的条目全部读不到（已删除）：无从针对，不做修正。"""
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(facts=["fact-a"]),
            _classify_payload({"e0": "other"}),
        ])
        _extractor(kv, llm).extract(
            [_source_unit()],
            context=_context(
                outcome=OUTCOME_FAILURE,
                retrieved=[{"id": "gone", "ref": "0.n"}],
                attribution={"gone": "harmful"},
            ),
        )
        assert len(llm.calls) == 2, "读不到条目不做修正"


# ====================================================================== #
# 三道闸
# ====================================================================== #


class TestGates:
    @staticmethod
    def test_desensitization_hit_drops_candidate():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(facts=["deploy as alice to prod"], tips=[]),
            _classify_payload({"e0": "other"}),
        ])
        source = _source_unit(user="alice")  # scope user 维参与脱敏检查
        units = _extractor(kv, llm).extract([source], context=_context())
        assert units == [], "命中主体标识的候选整条丢弃"

    @staticmethod
    def test_category_write_limit_drops_and_counts():
        kv = InMemoryKVStore()
        # 该类别已有一条生效条目；写入上限设为 1 → 新候选丢弃并记 dropped。
        UtilityStore(kv).add_category_count(EXP_SCOPE, "other", active=1)
        llm = ScriptedLLM([
            _extract_payload(facts=["fact-a"]),
            _classify_payload({"e0": "other"}),
        ])
        units = _extractor(kv, llm, category_write_limit=1).extract(
            [_source_unit()], context=_context()
        )
        assert units == [], "类别达写入上限的候选丢弃"
        counts = UtilityStore(kv).load_category_count(EXP_SCOPE, "other")
        assert counts == {"active": 1, "dropped": 1}, "丢弃计数加一"

    @staticmethod
    def test_default_category_limit_is_850():
        assert CATEGORY_WRITE_LIMIT == 850, "类别写入上限缺省 850"

    @staticmethod
    def test_executable_action_detection():
        assert is_executable_action("cp config/app.example.yaml config/app.yaml") is True, (
            "含命令判可执行"
        )
        assert is_executable_action("see https://example.com/docs for details") is True, (
            "含 URL 判可执行"
        )
        assert is_executable_action("prefer careful review before merging") is False, (
            "纯文本建议不可执行"
        )
        assert is_executable_action("") is False, "空动作不可执行"

    @staticmethod
    def test_non_executable_tip_flagged():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(tips=[{
                "condition": "before merging anything",
                "capability": "review",
                "action": "prefer careful review of the changes",
            }]),
            _classify_payload({"t0": "other"}),
        ])
        units = _extractor(kv, llm).extract([_source_unit()], context=_context())
        assert len(units) == 1, "前置：候选产出"
        assert units[0].system_metadata["executable"] is False, (
            "纯文本动作标记为不可执行"
        )


# ====================================================================== #
# 超时与解析
# ====================================================================== #


class TestDeadlineAndParsing:
    @staticmethod
    def test_time_limit_exceeded():
        kv = InMemoryKVStore()
        llm = ScriptedLLM()
        # 限值取负数等效「已超时」（Windows 时钟分辨率约 15ms，取 0 不稳定触发）。
        with pytest.raises(TimeoutError):
            _extractor(kv, llm, processing_time_limit=-1.0).extract(
                [_source_unit()], context=_context()
            )

    @staticmethod
    def test_llm_retry_then_success():
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            ValueError("boom"), _extract_payload(), _classify_payload({"e0": "other"}),
        ])
        extractor = _extractor(kv, llm)
        # 第一次调用失败重试：extract 用坏响应 + 好响应；空候选不再调分类。
        units = extractor.extract([_source_unit()], context=_context())
        assert units == [], "fact 列表为空（只测重试通路）"
        assert len(llm.calls) == 2, "失败一次重试一次后成功，无候选不分类"

    @staticmethod
    def test_markdown_fenced_response_parsed():
        """模型返回带 markdown 围栏的 JSON 也能解析（花括号段提取）。"""
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            "```json\n" + _extract_payload(facts=["fact-a"]) + "\n```",
            _classify_payload({"e0": "other"}),
        ])
        units = _extractor(kv, llm).extract([_source_unit()], context=_context())
        assert len(units) == 1 and units[0].content == "fact-a", "围栏内 JSON 正常解析"

    @staticmethod
    def test_blank_response_yields_empty_extraction():
        """模型对「无可提炼」返回空文本时按空 object 处理，不作解析错误。

        prompt 允许返回空列表，真实模型可能直接给空文本而非
        ``{"facts": [], "tips": []}``（e2e 实测 GLM-5.3 对全失败轨迹一致如此）——
        语义等同无候选，不触发重试也不炸链路。
        """
        kv = InMemoryKVStore()
        llm = ScriptedLLM([""])
        units = _extractor(kv, llm).extract([_source_unit()], context=_context())
        assert units == [], "空响应对应空候选"
        assert len(llm.calls) == 1, "空响应不是错误，不消耗重试"

    @staticmethod
    def test_tip_missing_segment_dropped():
        """tip 三段缺一（action 为空）时整条丢弃。"""
        kv = InMemoryKVStore()
        llm = ScriptedLLM([
            _extract_payload(tips=[{
                "condition": "c", "capability": "bash", "action": "",
            }]),
        ])
        units = _extractor(kv, llm).extract([_source_unit()], context=_context())
        assert units == [], "三段不齐的 tip 不产出候选"
