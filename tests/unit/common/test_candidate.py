"""CandidateSource——四类候选源的 dict DSL 序列化（candidate_to_dict / candidate_from_dict）。

契约：注册表持久化的形态就是 dict DSL（F04 D3——候选源是数据不是
代码），round-trip 必须无损；非法形态 ValidationError fail fast。
"""

# Pytest 类只用于分组，测试方法按 pytest 约定保留实例形态。
# pylint: disable=add-staticmethod-or-classmethod-decorator

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    FanOutCandidate,
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    IdsCandidate,
    PredicateCandidate,
    RecallCandidate,
    candidate_from_dict,
    candidate_to_dict,
)
from jiuwen_memory.common.type_def.candidate import (
    MAX_EXPLICIT_CANDIDATE_IDS,
    MAX_RECALL_TOP_K,
)

pytestmark = pytest.mark.unit

_T_INGEST_MS = 1_780_000_000_000  # 2026-05-29 附近（毫秒 epoch）


def test_explicit_candidate_limits_fail_fast() -> None:
    with pytest.raises(ValidationError, match="unit_ids 超过"):
        IdsCandidate(unit_ids=[f"u{i}" for i in range(MAX_EXPLICIT_CANDIDATE_IDS + 1)])
    with pytest.raises(ValidationError, match="top_k 超过"):
        RecallCandidate(query={"text": "x"}, top_k=MAX_RECALL_TOP_K + 1)


class TestKindRegistry:
    """kind 判别键 + 编解码注册表：序列化与分派共键，无 isinstance 阶梯。"""

    def test_kinds_match_dict_dsl_type(self) -> None:
        """kind 即 dict DSL 的 "type" 值——序列化与装配不可能漂移。"""
        assert PredicateCandidate.kind == "predicate"
        assert IdsCandidate.kind == "ids"
        assert RecallCandidate.kind == "recall"
        assert FanOutCandidate.kind == "fan_out"

    def test_to_dict_type_field_comes_from_kind(self) -> None:
        for source in (
            PredicateCandidate(),
            IdsCandidate(unit_ids=["a"]),
            RecallCandidate(query={"text": "x"}),
            FanOutCandidate(),
        ):
            assert candidate_to_dict(source)["type"] == source.kind, (
                f"{type(source).__name__} 的 type 字段必须来自 kind ClassVar"
            )

    def test_codec_registry_covers_all_kinds(self) -> None:
        """四型候选源均在 _CANDIDATE_CODECS 注册——分派只查表，无漏网。"""
        from jiuwen_memory.common.type_def.candidate import _CANDIDATE_CODECS

        assert set(_CANDIDATE_CODECS) == {
            "predicate",
            "ids",
            "recall",
            "fan_out",
        }, "kind 集合即 dict DSL 合法 type 集合"

    def test_unknown_object_rejected_by_registry(self) -> None:
        """未注册 kind 的对象：注册表查无此键，ValidationError（非 AttributeError）。"""
        with pytest.raises(ValidationError):
            candidate_to_dict(object())  # type: ignore[arg-type]


class TestPredicateRoundTrip:
    def test_default_is_all_source(self) -> None:
        """缺省谓词源 = 全量型（无 filters、无 window）。"""
        assert candidate_from_dict(candidate_to_dict(PredicateCandidate())) == (
            PredicateCandidate()
        )

    def test_with_filters_and_window(self) -> None:
        src = PredicateCandidate(
            filters=FilterGroup(
                logic=FilterLogic.AND,
                children=[
                    FilterClause("t_ingest", FilterOp.GTE, _T_INGEST_MS),
                    FilterClause("tier", FilterOp.EQ, "episodic"),
                ],
            ),
            window=3600,
        )
        assert candidate_from_dict(candidate_to_dict(src)) == src

    def test_nested_or_not_tree(self) -> None:
        src = PredicateCandidate(
            filters=FilterGroup(
                logic=FilterLogic.OR,
                children=[
                    FilterGroup(
                        logic=FilterLogic.NOT,
                        children=[FilterClause("source", FilterOp.EQ, "text")],
                    ),
                    FilterClause("tags", FilterOp.CONTAINS, "a"),
                ],
            )
        )
        assert candidate_from_dict(candidate_to_dict(src)) == src


class TestIdsRoundTrip:
    def test_point_source_round_trip(self) -> None:
        src = IdsCandidate(unit_ids=["u1", "u2"])
        assert candidate_from_dict(candidate_to_dict(src)) == src

    def test_empty_ids_rejected_at_boundary(self) -> None:
        with pytest.raises(ValidationError, match="unit_ids"):
            candidate_from_dict({"type": "ids", "unit_ids": []})

    def test_non_str_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unit_ids"):
            candidate_from_dict({"type": "ids", "unit_ids": ["u1", 2]})


class TestRecallRoundTrip:
    def test_recall_source_round_trip(self) -> None:
        src = RecallCandidate(
            query={"text": "项目背景"},
            channels=["hot"],
            top_k=10,
            filters=FilterClause("tier", FilterOp.EQ, "episodic"),
        )
        assert candidate_from_dict(candidate_to_dict(src)) == src

    def test_defaults_top_k_50(self) -> None:
        src = RecallCandidate(query={"text": "q"})
        assert candidate_from_dict(candidate_to_dict(src)) == src
        assert src.top_k == 50

    def test_query_must_be_dict(self) -> None:
        with pytest.raises(ValidationError, match="query"):
            candidate_from_dict({"type": "recall", "query": "text"})

    def test_missing_channels_defaults_to_empty(self) -> None:
        """channels 缺省 = 空列表（resolver 侧回落 Retriever 默认通道）。"""
        src = candidate_from_dict({"type": "recall", "query": {}})
        assert src.channels == []

    def test_top_k_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="top_k"):
            candidate_from_dict({"type": "recall", "query": {}, "top_k": 0})


class TestFanOutRoundTrip:
    def test_fanout_wraps_predicate_child(self) -> None:
        src = FanOutCandidate(
            child=PredicateCandidate(
                window=1800,
                filters=FilterClause("t_ingest", FilterOp.GTE, _T_INGEST_MS),
            )
        )
        assert candidate_from_dict(candidate_to_dict(src)) == src

    def test_fanout_shape_constraints_round_trip(self) -> None:
        """形状约束经 dict DSL round-trip 无损（注册表持久化依赖）。"""
        src = FanOutCandidate(
            child=PredicateCandidate(window=3600),
            require_empty=frozenset({"agent", "session"}),
            require_nonempty=frozenset({"user"}),
        )
        assert candidate_from_dict(candidate_to_dict(src)) == src

    def test_fanout_default_shape_is_unconstrained(self) -> None:
        """缺省空集 = 不约束（现行为，向后兼容锚点）。"""
        d = candidate_to_dict(FanOutCandidate())
        assert d["require_empty"] == []
        assert d["require_nonempty"] == []
        assert candidate_from_dict(d) == FanOutCandidate()

    def test_fanout_shape_from_json_list_form(self) -> None:
        """HTTP 端 JSON 只有 list——list 形态解析为 frozenset。"""
        src = candidate_from_dict(
            {
                "type": "fan_out",
                "child": {"type": "predicate"},
                "require_empty": ["agent", "session"],
            }
        )
        assert src.require_empty == frozenset({"agent", "session"})
        assert src.require_nonempty == frozenset()


class TestFanOutShapeValidation:
    """形状约束 fail-closed 校验——__post_init__ 覆盖所有构造路径。"""

    def test_unknown_scope_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="只允许 scope 字段"):
            FanOutCandidate(require_empty=frozenset({"org"}))

    def test_org_is_not_a_shape_field(self) -> None:
        """org 由父 scope 前缀约束，「org 为空」无业务语义——字段域外。"""
        with pytest.raises(ValidationError, match="只允许 scope 字段"):
            FanOutCandidate(require_nonempty=frozenset({"org"}))

    def test_overlapping_empty_and_nonempty_rejected(self) -> None:
        with pytest.raises(ValidationError, match="不能同时要求为空与非空"):
            FanOutCandidate(
                require_empty=frozenset({"user"}),
                require_nonempty=frozenset({"user"}),
            )

    def test_non_string_list_rejected_at_from_dict(self) -> None:
        """字符串会被 frozenset 静默拆成字符集——from_dict 边界先拦。"""
        with pytest.raises(ValidationError, match="require_empty"):
            candidate_from_dict(
                {"type": "fan_out", "child": {"type": "predicate"}, "require_empty": "user"}
            )

    def test_non_list_value_rejected(self) -> None:
        with pytest.raises(ValidationError, match="require_nonempty"):
            candidate_from_dict(
                {
                    "type": "fan_out",
                    "child": {"type": "predicate"},
                    "require_nonempty": 42,
                }
            )

    def test_non_predicate_child_type_is_rejected_without_scope_amplification(self) -> None:
        with pytest.raises(ValidationError, match="child.type"):
            candidate_from_dict(
                {
                    "type": "fan_out",
                    "child": {"type": "ids", "unit_ids": ["only-this"]},
                }
            )

    def test_python_child_must_also_be_predicate(self) -> None:
        with pytest.raises(ValidationError, match="PredicateCandidate"):
            FanOutCandidate(child=RecallCandidate(query={"text": "x"}))  # type: ignore[arg-type]


class TestFromDictValidation:
    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="未知的候选源 type"):
            candidate_from_dict({"type": "magic", "x": 1})

    def test_missing_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="未知的候选源 type"):
            candidate_from_dict({"filters": None})

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ValidationError, match="必须是对象"):
            candidate_from_dict(["type", "predicate"])  # type: ignore[arg-type]

    @pytest.mark.parametrize("window", [0, -1, True, "60"])
    def test_predicate_window_must_be_positive_integer(self, window) -> None:
        with pytest.raises(ValidationError, match="window"):
            candidate_from_dict({"type": "predicate", "window": window})

    def test_recall_top_k_rejects_bool(self) -> None:
        with pytest.raises(ValidationError, match="top_k"):
            candidate_from_dict(
                {"type": "recall", "query": {"text": "x"}, "top_k": True}
            )
