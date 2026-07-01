"""Extractor 单元测试（14 个测试）。

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
        {"source_id": "u1", "target": "fact", "content": "用户偏好 Python", "evidence": "偏好", "confidence": 1.0},
        {"source_id": "u2", "target": "event", "content": "系统报错", "evidence": "报错", "confidence": 0.9},
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
# T-E-09: FeatureExtractor 富化
# ---------------------------------------------------------------------------


def test_extract_feature_enrichment():
    """T-E-09: 含关键词 → tags 包含关键词（FeatureExtractor 富化）。"""
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


# ---------------------------------------------------------------------------
# T-E-13: 同主题合并（prompt 引导 LLM 合并咖啡相关事实）
# ---------------------------------------------------------------------------


def test_extract_prompt_includes_merge_rule():
    """T-E-13a: system prompt 含同主题合并规则，引导 LLM 产粗粒度事实。"""
    from construction.extractor_impl.llm_extractor import _EXTRACT_SYSTEM_PROMPT
    # 合并规则关键词必须在 prompt 里（防止后续误删）
    assert "MERGE same-topic" in _EXTRACT_SYSTEM_PROMPT
    assert "coffee" in _EXTRACT_SYSTEM_PROMPT.lower()  # 咖啡合并示例
    # 跨 source 不合并的约束
    assert "never merge across different sources" in _EXTRACT_SYSTEM_PROMPT


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
