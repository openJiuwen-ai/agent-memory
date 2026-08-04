"""Extractor 单元测试。

使用 MockLLM 隔离外部 LLM API 依赖。
"""

import json

import pytest

from common.type_def import (
    LifecycleState,
    MemoryTier,
)
from construction.extractor_impl.llm_extractor import (
    ExtractorImpl,
    InvalidExtractionCandidateError,
    InvalidExtractionJSONError,
)
from tests.unit.construction.fixtures import (
    MockLLM,
    create_test_unit,
)

# ---------------------------------------------------------------------------
# Helper: 创建 ExtractorImpl with MockLLM
# ---------------------------------------------------------------------------


def _make_extractor(llm_responses: list[str] | None = None) -> ExtractorImpl:
    """创建测试用 ExtractorImpl。"""
    return ExtractorImpl(
        llm=MockLLM(responses=llm_responses),
        min_confidence=0.5,
        retry_max_retries=3,
        retry_backoff_ms=1000,
    )


# ---------------------------------------------------------------------------
# T-E-01: 基本偏好提取
# ---------------------------------------------------------------------------


def test_extract_preference():
    """T-E-01: 含「偏好 Python」→ 派生 MemoryUnit(tier=SEMANTIC, provenance=[源id])。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "preference",
                        "content": "用户偏好用 Python 写代码",
                        "evidence": "偏好 Python",
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户偏好用 Python 写代码")]
    result = extractor.extract(units)

    assert len(result) >= 1
    derived = result[0]
    assert derived.tier == MemoryTier.SEMANTIC
    assert derived.provenance == ["u1"]
    assert "Python" in derived.content


# ---------------------------------------------------------------------------
# T-E-02: 基本事实提取
# ---------------------------------------------------------------------------


def test_extract_fact():
    """T-E-02: 含「确认」→ 派生 MemoryUnit(target=fact)。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "content": "系统已确认数据备份完成",
                        "evidence": "确认",
                        "confidence": 0.9,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "系统已确认数据备份完成")]
    result = extractor.extract(units)

    assert len(result) >= 1
    assert result[0].metadata.get("target") == "fact"


# ---------------------------------------------------------------------------
# T-E-03: 基本事件提取
# ---------------------------------------------------------------------------


def test_extract_event():
    """T-E-03: 含「昨天发生」→ 派生 MemoryUnit(target=event)。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "event",
                        "content": "昨天发生了数据库迁移",
                        "evidence": "昨天发生",
                        "confidence": 0.8,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "昨天发生了数据库迁移")]
    result = extractor.extract(units)

    assert len(result) >= 1
    assert result[0].metadata.get("target") == "event"


# ---------------------------------------------------------------------------
# T-E-04: LLM 返回空数组
# ---------------------------------------------------------------------------


def test_extract_llm_empty():
    """T-E-04: MockLLM 返回 [] → 空 list。"""
    extractor = _make_extractor(["[]"])
    units = [create_test_unit("u1", "这是一条普通消息")]
    result = extractor.extract(units)

    assert result == []


# ---------------------------------------------------------------------------
# T-E-05: 过滤 lifecycle≠ACTIVE
# ---------------------------------------------------------------------------


def test_extract_filter_lifecycle():
    """T-E-05: lifecycle=FORGOTTEN → 空 list。"""
    extractor = _make_extractor(["[]"])
    units = [create_test_unit("u1", "用户偏好 Python", lifecycle=LifecycleState.FORGOTTEN)]
    result = extractor.extract(units)

    assert result == []


# ---------------------------------------------------------------------------
# T-E-06: 过滤 content 空
# ---------------------------------------------------------------------------


def test_extract_filter_empty_content():
    """T-E-06: content="" → 空 list。"""
    extractor = _make_extractor(["[]"])
    units = [create_test_unit("u1", "")]
    result = extractor.extract(units)

    assert result == []


# ---------------------------------------------------------------------------
# T-E-07: 多 unit 批量提取
# ---------------------------------------------------------------------------


def test_extract_batch():
    """T-E-07: 3 条 unit 批量提取 → 一次 LLM 调用返回 3 条候选，各回指正确源 unit。"""
    # 批量提取：全部 unit 拼一个 prompt 一次调用，MockLLM 一次响应返回含全部候选的 JSON 数组。
    # 每条候选的 source_id 回指对应源 unit（u1/u2/u3），实现会校验 source_id 在本批 unit 内。
    payloads = [
        {
            "source_id": "u1",
            "target": "fact",
            "content": "用户偏好 Python",
            "evidence": "偏好",
            "confidence": 1.0,
        },
        {
            "source_id": "u2",
            "target": "event",
            "content": "系统报错",
            "evidence": "报错",
            "confidence": 0.9,
        },
        {
            "source_id": "u3",
            "target": "preference",
            "content": "用户喜欢简洁回答",
            "evidence": "喜欢",
            "confidence": 0.8,
        },
    ]
    responses = [json.dumps(payloads)]  # 一次调用返回全部 3 条候选
    extractor = _make_extractor(responses)
    units = [
        create_test_unit("u1", "用户偏好 Python"),
        create_test_unit("u2", "系统报错：内存泄漏"),
        create_test_unit("u3", "用户喜欢简洁回答"),
    ]
    result = extractor.extract(units)

    assert len(result) == 3
    # provenance 回指正确的源 unit
    provenance_ids = {r.provenance[0] for r in result}
    assert "u1" in provenance_ids
    assert "u2" in provenance_ids
    assert "u3" in provenance_ids


# ---------------------------------------------------------------------------
# T-E-08: provenance 回指
# ---------------------------------------------------------------------------


def test_extract_provenance():
    """T-E-08: 任意输入 → provenance=[源 unit id]。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "content": "用户偏好 Python",
                        "evidence": "偏好",
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户偏好 Python")]
    result = extractor.extract(units)

    assert len(result) >= 1
    assert result[0].provenance == ["u1"]


# ---------------------------------------------------------------------------
# T-E-09: LLM 抽 tag
# ---------------------------------------------------------------------------


def test_extract_tags_from_llm():
    """T-E-09: LLM 在候选里输出 tags → 派生 unit 的 tags 包含之（+ extracted 兜底）。

    tier/tags 均由 LLM 在同一 prompt 产出，不再走 FeatureExtractor 富化。
    """
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "preference",
                        "tier": "semantic",
                        "content": "用户偏好用 Python 写代码",
                        "tags": ["Python", "preference"],
                        "evidence": "偏好 Python",
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户偏好用 Python 写代码")]
    result = extractor.extract(units)

    assert len(result) >= 1
    derived = result[0]
    # LLM 抽的 tag 进了 tags（extracted 兜底也在）
    assert any("Python" in tag or "python" in tag.lower() for tag in derived.tags)
    assert "extracted" in derived.tags


# ---------------------------------------------------------------------------
# T-E-14: tier 由 LLM 判定
# ---------------------------------------------------------------------------


def test_extract_tier_by_llm():
    """T-E-14: LLM 输出 tier=episodic → 派生 unit tier==EPISODIC（不再恒为 SEMANTIC）。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "event",
                        "tier": "episodic",
                        "content": "昨天发生了数据库迁移",
                        "tags": ["database", "migration"],
                        "evidence": "昨天发生",
                        "confidence": 0.9,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "昨天发生了数据库迁移")]
    result = extractor.extract(units)

    assert len(result) == 1
    assert result[0].tier == MemoryTier.EPISODIC


def test_extract_tier_invalid_fallback_semantic():
    """T-E-15: LLM 输出越界 tier（如 core）→ 兜底 SEMANTIC，不产出 CORE/WORKING。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "tier": "core",  # 越界，抽取阶段不允许
                        "content": "用户是资深工程师",
                        "tags": ["role"],
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户是资深工程师")]
    result = extractor.extract(units)

    assert len(result) == 1
    assert result[0].tier == MemoryTier.SEMANTIC


# ---------------------------------------------------------------------------
# T-E-16: tags 清洗与截断
# ---------------------------------------------------------------------------


def test_extract_tags_sanitized_and_capped():
    """T-E-16: LLM 输出 4 个 tag（含空串/重复）→ 清洗去重后截断到 ≤3。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "tier": "semantic",
                        "content": "用户偏好 Python",
                        # 4 项：含一个重复、一个空串——去重去空后 3 个，再加 extracted 兜底
                        "tags": ["Python", "preference", "", "Python"],
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户偏好 Python")]
    result = extractor.extract(units)

    assert len(result) == 1
    tags = result[0].tags
    # LLM 抽的 tag 去重去空后 ≤3，extracted 兜底追加
    assert "Python" in tags
    assert "preference" in tags
    assert "extracted" in tags
    # 空串与重复不应出现
    assert "" not in tags
    assert tags.count("Python") == 1


def test_extract_inherits_write_tags():
    """派生 unit 合并源 unit 的 write tags，且不丢 LLM tags / extracted。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "tier": "semantic",
                        "content": "用户名叫张明",
                        "tags": ["name", "identity"],
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    source = create_test_unit("u1", "我叫张明")
    source.tags = ["profile"]
    result = extractor.extract([source])

    assert len(result) == 1
    tags = result[0].tags
    assert tags[0] == "profile", "write tags 应优先保留在前"
    assert "name" in tags
    assert "identity" in tags
    assert "extracted" in tags
    assert tags.count("profile") == 1


def test_extract_write_tags_dedup_with_extracted_marker():
    """源已含 extracted 时合并后不重复追加。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
                        "source_id": "u1",
                        "target": "fact",
                        "tier": "semantic",
                        "content": "用户喜欢苹果",
                        "tags": ["food"],
                        "confidence": 1.0,
                    }
                ]
            )
        ]
    )
    source = create_test_unit("u1", "喜欢苹果")
    source.tags = ["profile", "Extracted"]
    result = extractor.extract([source])

    assert len(result) == 1
    tags = result[0].tags
    assert "profile" in tags
    assert "food" in tags
    # 大小写不敏感去重：保留首次出现的写法
    assert sum(1 for t in tags if t.lower() == "extracted") == 1


def test_procedural_inherits_write_tags():
    """procedural 路径合并源 write tags + procedural 标记。"""
    extractor = _make_extractor(
        [json.dumps({"content": "目标:构建;步骤:npm run build;结果:成功"})]
    )
    source = create_test_unit("u1", "执行了 npm run build")
    source.tags = ["devops"]
    source.metadata = {"procedural": "true"}
    result = extractor.extract([source])

    assert len(result) == 1
    tags = result[0].tags
    assert tags == ["devops", "procedural"]


# ---------------------------------------------------------------------------
# T-E-10: confidence 过滤
# ---------------------------------------------------------------------------


def test_extract_confidence_filter():
    """T-E-10: LLM 返回 confidence=0.3 → 被过滤，不产出。"""
    extractor = _make_extractor(
        [
            json.dumps(
                    [
                        {
                            "source_id": "u1",
                            "target": "fact",
                            "content": "用户可能使用 Python",
                        "evidence": "可能",
                        "confidence": 0.3,
                    }
                ]
            )
        ]
    )
    units = [create_test_unit("u1", "用户可能使用 Python")]
    result = extractor.extract(units)

    assert result == []


# ---------------------------------------------------------------------------
# T-E-11: LLM 返回非 JSON
# ---------------------------------------------------------------------------


def test_extract_llm_non_json():
    """T-E-11: 非 JSON 是失败，不得伪装成模型明确返回空数组。"""
    extractor = _make_extractor(["This is not a JSON response"])
    units = [create_test_unit("u1", "用户偏好 Python")]

    with pytest.raises(InvalidExtractionJSONError) as exc_info:
        extractor.extract(units)

    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


# ---------------------------------------------------------------------------
# T-E-12: operator_type 和 health
# ---------------------------------------------------------------------------


def test_extractor_operator_type_and_health():
    """T-E-12: operator_type 返回 EXTRACTOR, health 返回 None。"""
    extractor = _make_extractor(["[]"])
    from construction.base import OperatorType

    assert extractor.operator_type() == OperatorType.EXTRACTOR
    assert extractor.health() is None


# ---------------------------------------------------------------------------
# T-E-13: 同主题合并（prompt 引导 LLM 合并咖啡相关事实）
# ---------------------------------------------------------------------------


def test_extract_prompt_includes_merge_rule():
    """T-E-13a: prompt 约束粒度双向——一 matter 不拆成碎片、二 matter 不在合并中消失。"""
    from construction.extractor_impl.llm_extractor import _EXTRACT_SYSTEM_PROMPT

    # 粒度双向约束：既不拆一件事成并列碎片，也不让第二件事消失进第一件
    assert "one matter per item" in _EXTRACT_SYSTEM_PROMPT
    assert "Do not split one matter into parallel fragments" in _EXTRACT_SYSTEM_PROMPT
    assert "do not let a second matter vanish into" in _EXTRACT_SYSTEM_PROMPT
    # 跨 source 不合并的约束
    assert "combine across source lines" in _EXTRACT_SYSTEM_PROMPT


def test_extract_merged_topic_produces_one_unit():
    """T-E-13b: LLM 返回合并后的 1 条（咖啡偏好合一条）→ Extractor 产出 1 条派生单元。"""
    extractor = _make_extractor([
        json.dumps([
            {
                "source_id": "u1",
                "target": "preference",
                "content": "Alice 的咖啡偏好：早上喝美式、下午喝拿铁，且不加糖",
                "evidence": "咖啡偏好",
                "confidence": 1.0,
            }
        ])
    ])
    units = [create_test_unit("u1", "Alice 早上喝美式，下午喝拿铁，不加糖")]
    result = extractor.extract(units)

    assert len(result) == 1
    assert "咖啡" in result[0].content
    assert "美式" in result[0].content
    assert "拿铁" in result[0].content
    assert "不加糖" in result[0].content


def test_extract_l2_is_compact_statement_with_source_reference():
    """派生 L2 只保存紧凑陈述，通过 source_ref/provenance 回指来源。"""
    source = "Alice owns two red bicycles and one blue bicycle."
    statement = "Alice owns three bicycles."
    extractor = _make_extractor([
        json.dumps([{
            "source_id": "u1",
            "target": "fact",
            "content": statement,
            "evidence": "two red bicycles and one blue bicycle",
            "confidence": 1.0,
        }])
    ])

    result = extractor.extract([create_test_unit("u1", source)])

    assert result[0].content == statement
    assert "Source:" not in result[0].content
    assert result[0].source_ref == "u1"
    assert result[0].provenance == ["u1"]
    assert result[0].metadata["extracted_statement"] == statement


def test_extract_skips_invalid_candidate_and_preserves_valid_candidate():
    extractor = _make_extractor([
        json.dumps([
            {
                "source_id": "u1",
                "target": "fact",
                "content": "valid statement",
                "confidence": 1.0,
            },
            {
                "source_id": "missing",
                "target": "fact",
                "content": "invalid statement",
                "confidence": 1.0,
            },
        ])
    ])

    result = extractor.extract([create_test_unit("u1", "source")])

    assert len(result) == 1
    assert result[0].content == "valid statement"
    assert result[0].provenance == ["u1"]


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"source_id": "u1", "content": "statement"},
        {"source_id": "u1", "content": "statement", "confidence": "nan"},
    ],
)
def test_extract_rejects_missing_or_invalid_confidence(item):
    extractor = _make_extractor([json.dumps([item])])

    with pytest.raises(InvalidExtractionCandidateError):
        extractor.extract([create_test_unit("u1", "source")])


def test_extract_continues_after_one_sub_batch_fails():
    extractor = _make_extractor([
        "not valid JSON",
        json.dumps([
            {
                "source_id": "u9",
                "target": "fact",
                "content": "valid statement from the second sub-batch",
                "confidence": 1.0,
            }
        ]),
    ])
    units = [create_test_unit(f"u{i}", f"source {i}") for i in range(1, 10)]

    result = extractor.extract(units)

    assert len(result) == 1
    assert result[0].content == "valid statement from the second sub-batch"
    assert result[0].provenance == ["u9"]


def test_extract_does_not_hide_failed_sub_batch_as_empty_result():
    extractor = _make_extractor(["not valid JSON", "[]"])
    units = [create_test_unit(f"u{i}", f"source {i}") for i in range(1, 10)]

    with pytest.raises(InvalidExtractionJSONError):
        extractor.extract(units)


def test_extract_deduplicates_same_statement_within_source():
    item = {
        "source_id": "u1",
        "target": "fact",
        "content": "Alice owns three bicycles.",
        "confidence": 1.0,
    }
    extractor = _make_extractor([json.dumps([item, item])])

    result = extractor.extract([create_test_unit("u1", "source")])

    assert len(result) == 1


def test_extract_preserves_structured_record_target():
    extractor = _make_extractor([
        json.dumps([{
            "source_id": "u1",
            "target": "structured_record",
            "content": "Sunday: Admon works from 8 a.m. to 4 p.m.",
            "evidence": "Sunday | Admon | 8am-4pm",
            "confidence": 1.0,
        }])
    ])

    result = extractor.extract([create_test_unit("u1", "Sunday | Admon | 8am-4pm")])

    assert result[0].metadata["target"] == "structured_record"


def test_keyword_procedural_inherits_write_tags():
    """keyword procedural 降级路径同样合并 write tags。"""
    from common.chunker.chunker_impl.recursive_chunker import RecursiveChunker
    from construction.extractor_impl.keyword_extractor import KeywordExtractor

    extractor = KeywordExtractor(RecursiveChunker(chunk_size_chars=200, overlap_chars=0))
    source = create_test_unit("u1", "执行了 npm run build")
    source.tags = ["devops"]
    source.metadata = {"procedural": "true"}
    result = extractor.extract([source])

    assert len(result) == 1
    assert result[0].tags == ["devops", "procedural"]
