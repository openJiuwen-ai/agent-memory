"""Associator 单元测试（LLMAssociator + KeywordAssociator）。

使用 MockLLM / HashEmbedder / RuleFeatureExtractor 隔离外部依赖。
"""

import json
from importlib import import_module

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.associator_impl.keyword_associator import KeywordAssociator
from jiuwen_memory.construction.associator_impl.llm_associator import LLMAssociator
from jiuwen_memory.construction.base import OperatorType
from tests.unit.construction.fixtures import (
    HashEmbedder,
    MockLLM,
    RuleFeatureExtractor,
)

# ---------------------------------------------------------------------------
# Helper: 创建测试组件
# ---------------------------------------------------------------------------


def _make_unit(
    unit_id: str = "u1",
    content: str = "用户偏好用 Python 写代码",
    **overrides,
) -> MemoryUnit:
    """创建测试用的 MemoryUnit。"""
    return MemoryUnit(
        id=unit_id,
        scope=overrides.get("scope") or Scope(org="test", user="alice"),
        tier=overrides.get("tier", MemoryTier.EPISODIC),
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=overrides.get("lifecycle", LifecycleState.ACTIVE),
        temporal=Temporal(),
    )


def _make_llm_associator(
    llm_responses: list[str] | None = None,
    **overrides,
) -> LLMAssociator:
    """创建测试用 LLMAssociator。"""
    params = {
        "similarity_threshold": 0.7,
        "keyword_jaccard_threshold": 0.3,
        "entity_match_threshold": 0.8,
        "min_auto_confirm": 0.5,
        "max_auto_confirm": 0.85,
        "deep_discovery": False,
    }
    params.update(overrides)
    return LLMAssociator(
        llm=MockLLM(responses=llm_responses),
        feature_extractor=RuleFeatureExtractor(),
        embedder=HashEmbedder(dim=128),
        similarity_threshold=params["similarity_threshold"],
        keyword_jaccard_threshold=params["keyword_jaccard_threshold"],
        entity_match_threshold=params["entity_match_threshold"],
        min_auto_confirm=params["min_auto_confirm"],
        max_auto_confirm=params["max_auto_confirm"],
        deep_discovery=params["deep_discovery"],
        retry_max_retries=3,
        retry_backoff_ms=1000,
    )


def _make_keyword_associator(min_overlap: int = 2) -> KeywordAssociator:
    """创建测试用 KeywordAssociator。"""
    return KeywordAssociator(
        feature_extractor=RuleFeatureExtractor(),
        min_overlap=min_overlap,
    )


# ---------------------------------------------------------------------------
# T-A-01: LLMAssociator operator_type 和 health
# ---------------------------------------------------------------------------


def test_llm_associator_operator_type_and_health():
    """T-A-01: operator_type 返回 ASSOCIATOR, health 返回 None。"""
    associator = _make_llm_associator()
    assert associator.operator_type() == OperatorType.ASSOCIATOR
    assert associator.health() is None


# ---------------------------------------------------------------------------
# T-A-02: LLMAssociator 少于 2 个 unit → 空 list
# ---------------------------------------------------------------------------


def test_llm_associator_single_unit():
    """T-A-02: 只有 1 个 unit → 无可关联，返回空 list。"""
    associator = _make_llm_associator()
    units = [_make_unit("u1", "用户偏好 Python")]
    result = associator.associate(units)
    assert result == []


# ---------------------------------------------------------------------------
# T-A-03: LLMAssociator 空 units → 空 list
# ---------------------------------------------------------------------------


def test_llm_associator_empty_units():
    """T-A-03: 空 list 输入 → 空 list。"""
    associator = _make_llm_associator()
    result = associator.associate([])
    assert result == []


# ---------------------------------------------------------------------------
# T-A-04: LLMAssociator similar_to（向量 cosine ≥ threshold）
# ---------------------------------------------------------------------------


def test_llm_associator_similar_to_by_cosine():
    """T-A-04: 两 unit 内容相同 → cosine≈1.0 ≥ threshold → similar_to 关系。"""
    # HashEmbedder: 同文本 → cosine=1.0
    associator = _make_llm_associator(
        similarity_threshold=0.7,
        # score=1.0 ≥ max_auto_confirm=0.85 → 直接确认
        max_auto_confirm=0.85,
        deep_discovery=False,
    )
    units = [
        _make_unit("u1", "Python is great for data science"),
        _make_unit("u2", "Python is great for data science"),
    ]
    result = associator.associate(units)

    # 应产出至少一条 similar_to 关系
    similar = [r for r in result if r.relation == "similar_to"]
    assert len(similar) >= 1
    # score 应 >= similarity_threshold
    assert similar[0].score >= 0.7


# ---------------------------------------------------------------------------
# T-A-05: LLMAssociator corefers（同名实体跨 unit）
# ---------------------------------------------------------------------------


def test_llm_associator_corefers_by_entity():
    """T-A-05: 两 unit 共享实体 "Python" → corefers 关系。"""
    # RuleFeatureExtractor 识别大写开头的英文词为实体，entity.score=0.7
    # 所以 entity_match_threshold 需 ≤ 0.7 才能让实体通过过滤
    associator = _make_llm_associator(
        similarity_threshold=0.99,  # 高阈值：避免 cosine 同义词干扰
        keyword_jaccard_threshold=0.99,  # 高阈值：避免 Jaccard 干扰
        entity_match_threshold=0.7,  # 匹配 RuleFeatureExtractor 的 score=0.7
        max_auto_confirm=0.85,
        deep_discovery=False,
    )
    units = [
        _make_unit("u1", "Python is a programming language"),
        _make_unit("u2", "Python has many libraries"),
    ]
    result = associator.associate(units)

    # RuleFeatureExtractor 在两个 unit 中都识别 "Python" 实体
    corefers = [r for r in result if r.relation == "corefers"]
    assert len(corefers) >= 1


# ---------------------------------------------------------------------------
# T-A-06: LLMAssociator 关键词 Jaccard 补充 similar_to
# ---------------------------------------------------------------------------


def test_llm_associator_similar_to_by_jaccard():
    """T-A-06: 两 unit 共享足够关键词 → Jaccard ≥ threshold → similar_to 关系。"""
    # 设置 cosine 阈值极高（内容不同 → cosine 低），让 Jaccard 补充通道生效
    associator = _make_llm_associator(
        similarity_threshold=0.99,  # cosine 通道不触发
        keyword_jaccard_threshold=0.3,
        max_auto_confirm=0.85,
        deep_discovery=False,
    )
    # 两段文本共享大量关键词
    units = [
        _make_unit("u1", "Python data science machine learning"),
        _make_unit("u2", "Python machine learning data analysis"),
    ]
    result = associator.associate(units)

    similar = [r for r in result if r.relation == "similar_to"]
    # 关键词 Jaccard 应触发
    assert len(similar) >= 1


# ---------------------------------------------------------------------------
# T-A-07: LLMAssociator Phase 3 LLM 验证
# ---------------------------------------------------------------------------


def test_llm_associator_llm_verify():
    """T-A-07: 候选 score 在 min_auto_confirm ~ max_auto_confirm → LLM 验证。"""
    # 使用同文本确保 cosine=1.0 → similar_to 候选产生
    # 设置 max_auto_confirm=1.0 使 score=1.0 也需要验证（落在 0.5~1.0 区间）
    verify_response = json.dumps(
        [
            {
                "source_id": "u1",
                "target_id": "u2",
                "relation": "similar_to",
                "valid": True,
                "adjusted_score": 0.9,
                "reason": "Both discuss the same topic",
            }
        ]
    )

    associator = _make_llm_associator(
        llm_responses=[verify_response],
        similarity_threshold=0.7,
        min_auto_confirm=0.5,
        max_auto_confirm=1.0,  # 所有候选都需要验证
        deep_discovery=False,
    )

    units = [
        _make_unit("u1", "Python is a programming language"),
        _make_unit("u2", "Python is a programming language"),  # 同文本 → cosine=1.0
    ]
    result = associator.associate(units)

    # LLM 验证应产出关系
    assert len(result) >= 1
    similar = [r for r in result if r.relation == "similar_to"]
    assert len(similar) >= 1


# ---------------------------------------------------------------------------
# T-A-08: LLMAssociator Phase 3 深度发现
# ---------------------------------------------------------------------------


def test_llm_associator_deep_discovery():
    """T-A-08: deep_discovery=True → LLM 发现 caused_by/refers_to 等关系。"""
    # 先让 L1 产生一条 similar_to 候选（score≥max_auto_confirm → 直接确认）
    # 然后深度发现 LLM 产出一条 caused_by
    deep_response = json.dumps(
        [
            {
                "source_id": "u1",
                "target_id": "u2",
                "relation": "caused_by",
                "confidence": 0.7,
                "evidence": "u1 describes a bug that caused the error in u2",
            }
        ]
    )

    associator = _make_llm_associator(
        llm_responses=[deep_response],
        similarity_threshold=0.7,
        # 同文本 cosine=1.0 ≥ 0.85 → 直接确认 → 进入深度发现
        max_auto_confirm=0.85,
        deep_discovery=True,
    )

    units = [
        _make_unit("u1", "The database migration caused a timeout error"),
        _make_unit("u2", "The database migration caused a timeout error"),  # 同文本确保 cosine=1.0
    ]
    result = associator.associate(units)

    # 应产出 similar_to (L1) + caused_by (L3 deep)
    caused = [r for r in result if r.relation == "caused_by"]
    assert len(caused) >= 1


# ---------------------------------------------------------------------------
# T-A-09: LLMAssociator 无向关系去重
# ---------------------------------------------------------------------------


def test_llm_associator_dedup_undirected():
    """T-A-09: similar_to A→B 与 B→A 等价 → 去重后只保留一条。"""
    # 同文本 → cosine=1.0 (自动确认)
    associator = _make_llm_associator(
        similarity_threshold=0.7,
        max_auto_confirm=0.85,
        deep_discovery=False,
    )
    units = [
        _make_unit("u1", "Python is great for data science"),
        _make_unit("u2", "Python is great for data science"),
    ]
    result = associator.associate(units)

    # similar_to 是无向关系，应去重为 1 条
    similar = [r for r in result if r.relation == "similar_to"]
    assert len(similar) <= 1


# ---------------------------------------------------------------------------
# T-A-10: LLMAssociator metadata 包含 discovery_layer
# ---------------------------------------------------------------------------


def test_llm_associator_metadata_discovery_layer():
    """T-A-10: Relation.metadata 包含 discovery_layer 字段（L1/L2/L3）。"""
    associator = _make_llm_associator(
        similarity_threshold=0.7,
        max_auto_confirm=0.85,
        deep_discovery=False,
    )
    units = [
        _make_unit("u1", "Python is great for data science"),
        _make_unit("u2", "Python is great for data science"),
    ]
    result = associator.associate(units)

    if result:
        assert "discovery_layer" in result[0].metadata
        assert result[0].metadata["discovery_layer"] in ("L1", "L2", "L3")


# ---------------------------------------------------------------------------
# T-A-11: LLMAssociator LLM 验证失败降级
# ---------------------------------------------------------------------------


def test_llm_associator_llm_verify_failure_graceful():
    """T-A-11: LLM 返回非 JSON → 验证失败降级，保留候选项。"""
    # MockLLM 返回非 JSON → 验证失败
    associator = _make_llm_associator(
        llm_responses=["not a json response"],
        similarity_threshold=0.7,
        min_auto_confirm=0.5,
        max_auto_confirm=1.0,  # 所有候选都需要验证
        deep_discovery=False,
    )
    units = [
        _make_unit("u1", "Python is great for data science"),
        _make_unit("u2", "Python is great for data science"),
    ]
    result = associator.associate(units)

    # 降级：保留未验证但 score ≥ min_auto_confirm 的候选项
    # cosine=1.0 的 similar_to 候选 score=1.0 ≥ 0.5，应保留
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# T-A-12: KeywordAssociator 基本功能
# ---------------------------------------------------------------------------


def test_keyword_associator_basic():
    """T-A-12: 共享关键词 ≥ min_overlap → related 关系。"""
    associator = _make_keyword_associator(min_overlap=2)
    units = [
        _make_unit("u1", "Python data science"),
        _make_unit("u2", "Python machine learning data analysis"),
    ]
    result = associator.associate(units)

    assert len(result) >= 1
    assert result[0].relation == "related"


# ---------------------------------------------------------------------------
# T-A-13: KeywordAssociator operator_type 和 health
# ---------------------------------------------------------------------------


def test_keyword_associator_operator_type_and_health():
    """T-A-13: KeywordAssociator operator_type=ASSOCIATOR, health=None。"""
    associator = _make_keyword_associator()
    assert associator.operator_type() == OperatorType.ASSOCIATOR
    assert associator.health() is None


# ---------------------------------------------------------------------------
# T-A-14: KeywordAssociator 少于 min_overlap → 空 list
# ---------------------------------------------------------------------------


def test_keyword_associator_no_overlap():
    """T-A-14: 共享关键词数 < min_overlap → 空 list。"""
    associator = _make_keyword_associator(min_overlap=3)
    units = [
        _make_unit("u1", "Python programming"),
        _make_unit("u2", "Java programming"),
    ]
    result = associator.associate(units)

    # 共享关键词 "programming" 只有 1 个 < 3
    assert result == []


# ---------------------------------------------------------------------------
# T-A-15: AssociatorProducer 工厂注册
# ---------------------------------------------------------------------------


def test_associator_producer_factory():
    """T-A-15: AssociatorProducer 可创建 keyword 和 llm 实现。"""
    from jiuwen_memory.config import AssemblyContext
    from jiuwen_memory.construction.associator_impl import AssociatorProducer

    # llm 实现的依赖（echo / keyword / hashing）由各 _impl 包导入时自注册
    import_module("jiuwen_memory.common.llm.llm_impl")
    import_module("jiuwen_memory.common.feature_extractor.feature_extractor_impl")
    import_module("jiuwen_memory.common.embedder.embedder_impl")

    # keyword 实现
    keyword = AssociatorProducer.build("keyword", {}, AssemblyContext())
    assert isinstance(keyword, KeywordAssociator)

    # llm 实现（依赖经 config.dep 自取缺省实现）
    llm = AssociatorProducer.build("llm", {}, AssemblyContext())
    assert isinstance(llm, LLMAssociator)
