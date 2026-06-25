"""Extractor 单元测试（12 个测试）。

使用 MockLLM 隔离外部 LLM API 依赖。
"""

import json

from common.type_def import (
    LifecycleState,
    MemoryTier,
)
from construction.extractor_impl.llm_extractor import ExtractorImpl
from tests.unit.construction.fixtures import (
    MockLLM,
    RuleFeatureExtractor,
    create_test_unit,
)

# ---------------------------------------------------------------------------
# Helper: 创建 ExtractorImpl with MockLLM
# ---------------------------------------------------------------------------


def _make_extractor(llm_responses: list[str] | None = None) -> ExtractorImpl:
    """创建测试用 ExtractorImpl。"""
    return ExtractorImpl(
        llm=MockLLM(responses=llm_responses),
        feature_extractor=RuleFeatureExtractor(),
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
    """T-E-07: 3 条 unit → 每条独立提取。"""
    # 逐条提取：MockLLM 按调用次序循环返回，每条 unit 一次调用、各产出一条候选。
    payloads = [
        {"target": "fact", "content": "用户偏好 Python", "evidence": "偏好", "confidence": 1.0},
        {"target": "event", "content": "系统报错", "evidence": "报错", "confidence": 0.9},
        {
            "target": "preference",
            "content": "用户喜欢简洁回答",
            "evidence": "喜欢",
            "confidence": 0.8,
        },
    ]
    responses = [json.dumps([payload]) for payload in payloads]
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
# T-E-09: FeatureExtractor 富化
# ---------------------------------------------------------------------------


def test_extract_feature_enrichment():
    """T-E-09: 含关键词 → tags 包含关键词（FeatureExtractor 富化）。"""
    extractor = _make_extractor(
        [
            json.dumps(
                [
                    {
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
    # RuleFeatureExtractor 会提取关键词
    derived = result[0]
    # 至少应该有一些 tags（关键词提取）
    # RuleFeatureExtractor 提取英文词 "Python"
    assert any("Python" in tag or "python" in tag.lower() for tag in derived.tags)


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
    """T-E-11: MockLLM 返回纯文本 → 解析失败，返回空 list。"""
    extractor = _make_extractor(["This is not a JSON response"])
    units = [create_test_unit("u1", "用户偏好 Python")]
    result = extractor.extract(units)

    assert result == []


# ---------------------------------------------------------------------------
# T-E-12: operator_type 和 health
# ---------------------------------------------------------------------------


def test_extractor_operator_type_and_health():
    """T-E-12: operator_type 返回 EXTRACTOR, health 返回 None。"""
    extractor = _make_extractor(["[]"])
    from construction.base import OperatorType

    assert extractor.operator_type() == OperatorType.EXTRACTOR
    assert extractor.health() is None
