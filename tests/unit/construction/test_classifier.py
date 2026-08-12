"""Classifier 单元测试（LLMClassifier + KeywordClassifier）。

LLMClassifier 重写为纯 LLM tier+tags 抽取后，测试覆盖：
- 单条/批量 classify → LLM 返 tier+tags JSON → 写回 unit.tier/tags
- tier 限定 episodic/semantic/procedural，非法值兜底 EPISODIC
- tags 清洗（去空/去重/去纯数字/截断 ≤3）
- LLM 返回非 JSON → 降级空 tags + EPISODIC，不崩
- 空 units / 非 ACTIVE / 空 content 跳过
"""

import json

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.classifier_impl.keyword_classifier import KeywordClassifier
from jiuwen_memory.construction.classifier_impl.llm_classifier import LLMClassifier
from tests.unit.construction.fixtures import MockLLM


def _make_unit(
    unit_id: str = "u1",
    content: str = "x",
    *,
    tags_override: list[str] | None = None,
    **overrides,
) -> MemoryUnit:
    """构造测试 unit：默认 EPISODIC/ACTIVE，支持 tags_override 与 scope/tier/lifecycle/provenance 覆盖。"""
    unit = MemoryUnit(
        id=unit_id,
        scope=overrides.get("scope") or Scope(org="test", user="alice"),
        tier=overrides.get("tier", MemoryTier.EPISODIC),
        segments=[Segment(content=content, source=Modality.TEXT)],
        lifecycle=overrides.get("lifecycle", LifecycleState.ACTIVE),
        temporal=Temporal(),
        provenance=overrides.get("provenance") or [],
    )
    if tags_override is not None:
        unit.tags = list(tags_override)
    return unit


def _make_classifier(llm_responses: list[str] | None = None) -> LLMClassifier:
    return LLMClassifier(
        llm=MockLLM(responses=llm_responses),
        retry_max_retries=3,
        retry_backoff_ms=1000,
    )


def _classify_response(items: list[dict]) -> str:
    """构造 LLM 返回的 JSON 数组字符串。"""
    return json.dumps(items)


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def test_operator_type_and_health():
    clf = _make_classifier()
    assert clf.operator_type() == OperatorType.CLASSIFIER
    assert clf.health() is None


def test_empty_units():
    clf = _make_classifier()
    assert clf.classify([]) == []


# ---------------------------------------------------------------------------
# 单条/批量 classify
# ---------------------------------------------------------------------------


def test_single_unit_tier_and_tags():
    """单条 → LLM 返 tier=semantic tags=[python,preference] → 写回 unit。"""
    resp = _classify_response([
        {"source_id": "u1", "tier": "semantic", "tags": ["python", "preference"]},
    ])
    clf = _make_classifier([resp])
    unit = _make_unit("u1", "用户偏好用 Python 写代码")
    result = clf.classify([unit])
    assert result[0].tier == MemoryTier.SEMANTIC
    assert "python" in [t.lower() for t in result[0].tags]
    assert "preference" in [t.lower() for t in result[0].tags]


def test_batch_classify_preserves_order():
    """3 条批量 → 一次 LLM 调用返 3 条，各 tier/tags 回写正确源 unit。"""
    resp = _classify_response([
        {"source_id": "u1", "tier": "semantic", "tags": ["coffee"]},
        {"source_id": "u2", "tier": "episodic", "tags": ["meeting"]},
        {"source_id": "u3", "tier": "procedural", "tags": ["deploy"]},
    ])
    clf = _make_classifier([resp])
    units = [
        _make_unit("u1", "Alice 喜欢咖啡"),
        _make_unit("u2", "Alice 参加了会议"),
        _make_unit("u3", "Alice 部署了服务"),
    ]
    result = clf.classify(units)
    by_id = {u.id: u for u in result}
    assert by_id["u1"].tier == MemoryTier.SEMANTIC
    assert by_id["u2"].tier == MemoryTier.EPISODIC
    assert by_id["u3"].tier == MemoryTier.PROCEDURAL


# ---------------------------------------------------------------------------
# tier 兜底
# ---------------------------------------------------------------------------


def test_unknown_tier_fallback_episodic():
    """LLM 返非法 tier（core）→ 兜底 EPISODIC。"""
    resp = _classify_response([{"source_id": "u1", "tier": "core", "tags": []}])
    clf = _make_classifier([resp])
    unit = _make_unit("u1")
    clf.classify([unit])
    assert unit.tier == MemoryTier.EPISODIC


def test_missing_tier_fallback_episodic():
    """LLM 缺失 tier → 兜底 EPISODIC。"""
    resp = _classify_response([{"source_id": "u1", "tags": ["x"]}])
    clf = _make_classifier([resp])
    unit = _make_unit("u1")
    clf.classify([unit])
    assert unit.tier == MemoryTier.EPISODIC


# ---------------------------------------------------------------------------
# tags 清洗
# ---------------------------------------------------------------------------


def test_tags_dedup_and_truncate():
    """tags 去重 + 截断到 ≤3。"""
    resp = _classify_response([
        {"source_id": "u1", "tier": "semantic", "tags": ["python", "Python", "code", "extra"]},
    ])
    clf = _make_classifier([resp])
    unit = _make_unit("u1")
    clf.classify([unit])
    assert len(unit.tags) <= 3
    lower_tags = [t.lower() for t in unit.tags]
    assert len(lower_tags) == len(set(lower_tags))  # 无重复


def test_tags_skip_empty_and_digit():
    """tags 跳过空串/纯数字。"""
    resp = _classify_response([
        {"source_id": "u1", "tier": "semantic", "tags": ["", "123", "python"]},
    ])
    clf = _make_classifier([resp])
    unit = _make_unit("u1")
    clf.classify([unit])
    assert "python" in [t.lower() for t in unit.tags]
    assert "123" not in unit.tags
    assert "" not in unit.tags


def test_tags_merge_with_existing():
    """tags 追加到已有 tags（去重）。"""
    resp = _classify_response([
        {"source_id": "u1", "tier": "semantic", "tags": ["python", "preference"]},
    ])
    clf = _make_classifier([resp])
    unit = _make_unit("u1", tags_override=["existing"])
    clf.classify([unit])
    lower = [t.lower() for t in unit.tags]
    assert "existing" in lower
    assert "python" in lower


# ---------------------------------------------------------------------------
# LLM 失败降级
# ---------------------------------------------------------------------------


def test_non_json_response_fallback():
    """LLM 返非 JSON → 降级空 tags + tier 保持/兜底 EPISODIC，不崩。"""
    clf = _make_classifier(["this is not json"])
    unit = _make_unit("u1", "some content")
    clf.classify([unit])
    # 非 JSON 解析失败 → tier 兜底 EPISODIC，tags 空
    assert unit.tier == MemoryTier.EPISODIC
    assert unit.tags == []


def test_skips_non_active_and_empty_content():
    """非 ACTIVE / 空 content 的 unit 跳过（不调 LLM）。"""
    resp = _classify_response([])
    clf = _make_classifier([resp])
    u_active = _make_unit("u1", "active content")
    u_inactive = _make_unit("u2", "inactive")
    u_inactive.lifecycle = LifecycleState.FORGOTTEN
    u_empty = _make_unit("u3", "")
    result = clf.classify([u_active, u_inactive, u_empty])
    # 只 active+非空 进 LLM 批次（u_active），其余原样返回未改
    assert len(result) == 3


# ---------------------------------------------------------------------------
# KeywordClassifier（保留既有规则实现）
# ---------------------------------------------------------------------------


def test_keyword_classifier_basic():
    clf = KeywordClassifier()
    assert clf.operator_type() == OperatorType.CLASSIFIER
    assert clf.health() is None
