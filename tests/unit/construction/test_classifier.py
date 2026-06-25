"""Classifier 单元测试（LLMClassifier + KeywordClassifier）。

使用 MockLLM / RuleFeatureExtractor 隔离外部依赖。
"""

import json
from importlib import import_module

from common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from construction.base import OperatorType
from construction.classifier_impl.keyword_classifier import KeywordClassifier
from construction.classifier_impl.llm_classifier import LLMClassifier
from tests.unit.construction.fixtures import (
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
        provenance=overrides.get("provenance") or [],
    )


def _make_llm_classifier(
    llm_responses: list[str] | None = None,
    llm_enabled: bool = True,
) -> LLMClassifier:
    """创建测试用 LLMClassifier。"""
    return LLMClassifier(
        llm=MockLLM(responses=llm_responses),
        feature_extractor=RuleFeatureExtractor(),
        llm_enabled=llm_enabled,
        max_units_per_llm_call=10,
        max_units_per_classify=50,
        retry_max_retries=3,
        retry_backoff_ms=1000,
    )


def _make_keyword_classifier() -> KeywordClassifier:
    """创建测试用 KeywordClassifier。"""
    return KeywordClassifier()


# ---------------------------------------------------------------------------
# T-C-01: LLMClassifier operator_type 和 health
# ---------------------------------------------------------------------------


def test_llm_classifier_operator_type_and_health():
    """T-C-01: operator_type 返回 CLASSIFIER, health 返回 None。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    assert classifier.operator_type() == OperatorType.CLASSIFIER
    assert classifier.health() is None


# ---------------------------------------------------------------------------
# T-C-02: LLMClassifier 空 units → 空 list
# ---------------------------------------------------------------------------


def test_llm_classifier_empty_units():
    """T-C-02: 空 list 输入 → 返回空 list。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    result = classifier.classify([])
    assert result == []


# ---------------------------------------------------------------------------
# T-C-03: 规则 tier 分类 — 偏好词 → CORE
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_tier_core():
    """T-C-03: 含「偏好」偏好词（≥min_match=1）→ CORE。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "用户偏好用 Python")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.CORE
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "rule"


# ---------------------------------------------------------------------------
# T-C-04: 规则 tier 分类 — 流程词 → PROCEDURAL
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_tier_procedural():
    """T-C-04: 含「步骤」1个流程词（≥min_match=1）→ PROCEDURAL。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "调试的步骤是先检查日志")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.PROCEDURAL
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "rule"


# ---------------------------------------------------------------------------
# T-C-05: 规则 tier 分类 — 概念词 → SEMANTIC
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_tier_semantic():
    """T-C-05: 含「定义」1个概念词（≥min_match=1）→ SEMANTIC。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "GIL 的定义是什么")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.SEMANTIC
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "rule"


# ---------------------------------------------------------------------------
# T-C-06: 规则 tier 分类 — 无关键词 → 默认 EPISODIC
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_tier_default_episodic():
    """T-C-06: 无关键词触发 → source 映射 TEXT→EPISODIC。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "今天开了一个团队会议")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.EPISODIC
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "default"


# ---------------------------------------------------------------------------
# T-C-07: 规则 topic 分类 — 技术关键词 → "技术" 标签
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_topic():
    """T-C-07: 含技术关键词 → tags 包含 "技术"。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "Python API 后端部署")]
    result = classifier.classify(units)

    assert "技术" in result[0].tags


# ---------------------------------------------------------------------------
# T-C-08: 规则 importance 分类 — boost 关键词
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_importance_boost():
    """T-C-08: 含「核心」boost 关键词 → importance > baseline。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "这是核心功能，必须完成")]
    result = classifier.classify(units)

    importance = float(result[0].metadata.get("importance", "0"))
    # TEXT baseline=0.3, boost=+0.2 → ≥0.5
    assert importance >= 0.5


# ---------------------------------------------------------------------------
# T-C-09: 规则 importance 分类 — suppress 关键词
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_importance_suppress():
    """T-C-09: 含「大概」suppress 关键词 → importance < baseline。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "大概可以试试这种做法")]
    result = classifier.classify(units)

    importance = float(result[0].metadata.get("importance", "0"))
    # TEXT baseline=0.3, suppress=-0.1 → 0.2
    assert importance <= 0.3


# ---------------------------------------------------------------------------
# T-C-10: 规则 confidence 分类 — 直接陈述词
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_confidence_direct():
    """T-C-10: 含「确认」直接陈述词 → confidence=0.9。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "系统已确认数据备份完成")]
    result = classifier.classify(units)

    confidence = float(result[0].metadata.get("confidence", "0"))
    assert confidence == 0.9


# ---------------------------------------------------------------------------
# T-C-11: 规则 freshness 分类 — metadata 写入
# ---------------------------------------------------------------------------


def test_llm_classifier_rule_freshness():
    """T-C-11: 任何 unit → metadata 包含 freshness 字段（hot/warm/cold）。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "用户偏好用 Python")]
    result = classifier.classify(units)

    freshness = result[0].metadata.get("freshness", "")
    assert freshness in ("hot", "warm", "cold")


# ---------------------------------------------------------------------------
# T-C-12: provenance 映射 — 派生 unit 保留已有 tier
# ---------------------------------------------------------------------------


def test_llm_classifier_provenance_tier():
    """T-C-12: 有 provenance 且 tier≠EPISODIC → 保留已有 tier。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "Python GIL 机制", tier=MemoryTier.SEMANTIC, provenance=["src1"])]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.SEMANTIC
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "provenance"


# ---------------------------------------------------------------------------
# T-C-13: LLM 深度分类 — 覆盖规则 default tier
# ---------------------------------------------------------------------------


def test_llm_classifier_llm_override_tier():
    """T-C-13: 规则 tier=default → LLM 覆盖为 semantic。"""
    llm_response = json.dumps(
        [
            {
                "unit_id": "u1",
                "tier": "semantic",
                "topics": ["技术"],
                "importance": 0.6,
                "confidence": 0.8,
                "freshness": None,
            }
        ]
    )

    classifier = _make_llm_classifier(llm_responses=[llm_response], llm_enabled=True)
    # "今天开了一个团队会议" — 无关键词触发 → 规则 tier=default → LLM 补充
    units = [_make_unit("u1", "今天开了一个团队会议")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.SEMANTIC
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "llm"


# ---------------------------------------------------------------------------
# T-C-14: LLM 深度分类 — LLM 返回 null 保留规则结果
# ---------------------------------------------------------------------------


def test_llm_classifier_llm_null_preserves_rule():
    """T-C-14: LLM 对规则 tier=default 的维度返回 null → 保留 EPISODIC 默认。"""
    # 使用无关键词内容 → 规则 tier=default(EPISODIC)
    # LLM tier=null → 保留 EPISODIC；LLM topic=["工作"] 覆盖规则 topic
    llm_response = json.dumps(
        [
            {
                "unit_id": "u1",
                "tier": None,  # null → 保留规则 default EPISODIC
                "topics": ["工作"],
                "importance": None,  # 保留规则
                "confidence": 0.8,  # LLM 覆盖（虽然 confidence source 是 rule，LLM 可修正）
                "freshness": None,
            }
        ]
    )

    classifier = _make_llm_classifier(llm_responses=[llm_response], llm_enabled=True)
    units = [_make_unit("u1", "今天开了一个团队会议")]
    result = classifier.classify(units)

    # 规则 tier=default(EPISODIC)，LLM null → 保留 EPISODIC
    assert result[0].tier == MemoryTier.EPISODIC
    source = json.loads(result[0].metadata.get("classify_source", "{}"))
    assert source.get("tier") == "default"


# ---------------------------------------------------------------------------
# T-C-15: LLM 失败降级 — 保留纯规则结果
# ---------------------------------------------------------------------------


def test_llm_classifier_llm_failure_graceful():
    """T-C-15: LLM 返回非 JSON → 降级，保留纯规则结果。"""
    classifier = _make_llm_classifier(llm_responses=["not a json"], llm_enabled=True)
    units = [_make_unit("u1", "用户偏好用 Python")]
    result = classifier.classify(units)

    # 规则 tier=CORE（"偏好" 触发）仍然生效
    assert result[0].tier == MemoryTier.CORE
    # metadata 仍有完整五维分类
    assert "importance" in result[0].metadata
    assert "confidence" in result[0].metadata
    assert "freshness" in result[0].metadata


# ---------------------------------------------------------------------------
# T-C-16: KeywordClassifier 基本功能
# ---------------------------------------------------------------------------


def test_keyword_classifier_basic():
    """T-C-16: 含偏好词 → SEMANTIC tier + topic 标签。"""
    classifier = _make_keyword_classifier()
    units = [_make_unit("u1", "用户偏好用 Python")]
    result = classifier.classify(units)

    assert result[0].tier == MemoryTier.SEMANTIC


# ---------------------------------------------------------------------------
# T-C-17: KeywordClassifier operator_type 和 health
# ---------------------------------------------------------------------------


def test_keyword_classifier_operator_type_and_health():
    """T-C-17: KeywordClassifier operator_type=CLASSIFIER, health=None。"""
    classifier = _make_keyword_classifier()
    assert classifier.operator_type() == OperatorType.CLASSIFIER
    assert classifier.health() is None


# ---------------------------------------------------------------------------
# T-C-18: ClassifierProducer 工厂注册
# ---------------------------------------------------------------------------


def test_classifier_producer_factory():
    """T-C-18: ClassifierProducer 可创建 keyword 和 llm 实现。"""
    from config import AssemblyContext
    from construction.classifier_impl import ClassifierProducer

    # llm 实现的依赖（echo / keyword）由各 _impl 包导入时自注册
    import_module("common.llm.llm_impl")
    import_module("common.feature_extractor.feature_extractor_impl")

    # keyword 实现
    keyword = ClassifierProducer.build("keyword", {}, AssemblyContext())
    assert isinstance(keyword, KeywordClassifier)

    # llm 实现（依赖经 config.dep 自取缺省实现）
    llm = ClassifierProducer.build("llm", {}, AssemblyContext())
    assert isinstance(llm, LLMClassifier)


# ---------------------------------------------------------------------------
# T-C-19: LLM 禁用时纯规则分类
# ---------------------------------------------------------------------------


def test_llm_classifier_disabled_pure_rule():
    """T-C-19: llm_enabled=False → 纯规则分类，不调 LLM。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [
        _make_unit("u1", "用户偏好用 Python"),
        _make_unit("u2", "调试的步骤是先检查日志"),
    ]
    result = classifier.classify(units)

    assert len(result) == 2
    # "偏好" → CORE (min_match=1)
    assert result[0].tier == MemoryTier.CORE
    # "步骤" → PROCEDURAL
    assert result[1].tier == MemoryTier.PROCEDURAL


# ---------------------------------------------------------------------------
# T-C-20: 五维 metadata 完整性
# ---------------------------------------------------------------------------


def test_llm_classifier_metadata_completeness():
    """T-C-20: 分类后 metadata 包含 importance/confidence/freshness/classify_source。"""
    classifier = _make_llm_classifier(llm_enabled=False)
    units = [_make_unit("u1", "用户偏好用 Python")]
    result = classifier.classify(units)

    assert "importance" in result[0].metadata
    assert "confidence" in result[0].metadata
    assert "freshness" in result[0].metadata
    assert "classify_source" in result[0].metadata

    # classify_source 是 JSON 字符串
    source = json.loads(result[0].metadata["classify_source"])
    assert "tier" in source
    assert "topic" in source
    assert "importance" in source
    assert "confidence" in source
    assert "freshness" in source
