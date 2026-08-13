"""UnitReader（scope 内点读）+ 后置过滤（passes / in_event_window / matches_filters）测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.type_def import (
    T_INVALID_OPEN,
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    memory_key,
    normalize,
)
from jiuwen_memory.common.type_def.memory import LifecycleState
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.retrieval.retriever_impl.unit_reader import (
    UnitReader,
    in_event_window,
    matches_filters,
    passes,
    valid_at,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


def test_current_query_allows_active_only(unit_factory) -> None:
    assert passes(unit_factory("a", "x", lifecycle=LifecycleState.ACTIVE), None)
    assert not passes(unit_factory("s", "x", lifecycle=LifecycleState.SUPERSEDED), None)
    assert not passes(unit_factory("r", "x", lifecycle=LifecycleState.ARCHIVED), None)


def test_include_archived_allows_archived(unit_factory) -> None:
    unit = unit_factory("r", "x", lifecycle=LifecycleState.ARCHIVED)

    assert passes(unit, None, include_archived=True)


def test_historical_query_excludes_forgotten(unit_factory) -> None:
    assert not passes(unit_factory("f", "x", lifecycle=LifecycleState.FORGOTTEN), NOW)
    assert passes(unit_factory("a", "x", lifecycle=LifecycleState.ACTIVE), NOW)


def test_valid_time_window_excludes_not_yet_effective(unit_factory) -> None:
    future = unit_factory("u", "x", t_valid=NOW + timedelta(days=1))

    assert not passes(future, NOW)


# -- event-time 窗（in_event_window） ----------------------------------------- #


def test_event_window_filters_by_t_event(unit_factory) -> None:
    lo, hi = NOW - timedelta(days=1), NOW + timedelta(days=1)
    inside = unit_factory("in", "x", t_event=NOW)
    before = unit_factory("be", "x", t_event=NOW - timedelta(days=3))

    assert in_event_window(inside, lo, hi)
    assert not in_event_window(before, lo, hi)


def test_event_window_is_half_open(unit_factory) -> None:
    lo, hi = NOW - timedelta(days=1), NOW
    on_upper = unit_factory("u", "x", t_event=NOW)  # 上界 exclusive

    assert not in_event_window(on_upper, lo, hi)


def test_event_window_lenient_when_no_t_event(unit_factory) -> None:
    # 缺 t_event：不据此丢弃（宽松兜底，避免 over-drop）
    assert in_event_window(unit_factory("n", "x"), NOW - timedelta(days=1), NOW + timedelta(days=1))


def test_event_window_lenient_when_naive_t_event(unit_factory) -> None:
    # t_event 为 naive datetime（写入路径未做 UTC 归一化，issue #91）：与 aware 窗口
    # 比较会抛 TypeError，按宽松不丢弃返回 True，避免 over-drop 伤召回。
    unit = unit_factory("naive", "x", t_event=datetime(2026, 6, 16))
    assert unit.temporal.t_event.tzinfo is None
    assert in_event_window(unit, NOW - timedelta(days=1), NOW + timedelta(days=1))


def test_event_window_noop_without_constraint(unit_factory) -> None:
    assert in_event_window(unit_factory("a", "x", t_event=NOW), None, None)


# -- 调用方显式 filters（matches_filters） ------------------------------------ #


def test_filter_tags_contains(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["work", "coffee"])

    assert matches_filters(unit, FilterClause("tags", FilterOp.CONTAINS, "coffee"))
    assert not matches_filters(unit, FilterClause("tags", FilterOp.CONTAINS, "tea"))


def test_filter_scalar_field_eq_and_in(unit_factory) -> None:
    unit = unit_factory("a", "x")  # tier=SEMANTIC（conftest make_unit 固定）

    assert matches_filters(unit, FilterClause("tier", FilterOp.EQ, "semantic"))
    assert matches_filters(unit, FilterClause("tier", FilterOp.IN, ["semantic", "core"]))
    assert not matches_filters(unit, FilterClause("tier", FilterOp.EQ, "core"))


def test_filter_missing_field_only_negative_ops_pass(unit_factory) -> None:
    unit = unit_factory("a", "x")  # 无 "priority" metadata

    assert matches_filters(unit, FilterClause("priority", FilterOp.NE, "high"))
    assert not matches_filters(unit, FilterClause("priority", FilterOp.EQ, "high"))


def test_filter_and_combination(unit_factory) -> None:
    # 内核只接受规范化后的 FilterExpr：旧扁平 list 经 normalize 收口
    unit = unit_factory("a", "x", tags=["coffee"])

    assert matches_filters(
        unit,
        normalize(
            [
                FilterClause("tags", FilterOp.CONTAINS, "coffee"),
                FilterClause("tier", FilterOp.EQ, "semantic"),
            ]
        ),
    )
    assert not matches_filters(
        unit,
        normalize(
            [
                FilterClause("tags", FilterOp.CONTAINS, "coffee"),
                FilterClause("tier", FilterOp.EQ, "core"),  # 一条不满足 → 整体不通过
            ]
        ),
    )


# -- 树形 filters（matches_filters × FilterGroup） --------------------------- #


def test_filter_tree_and_or_not_nested(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["coffee"])  # tier=semantic, lifecycle=ACTIVE
    expr = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("tier", FilterOp.EQ, "semantic"),
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("tags", FilterOp.CONTAINS, "coffee"),
                    FilterClause("tags", FilterOp.CONTAINS, "tea"),
                ],
            ),
            FilterGroup(FilterLogic.NOT, [FilterClause("lifecycle", FilterOp.EQ, "archived")]),
        ],
    )

    assert matches_filters(unit, expr), "tier 命中 + OR 命中 coffee + 非 archived → 通过"


def test_filter_tree_and_branch_failure_rejects(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["coffee"])
    expr = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("tier", FilterOp.EQ, "core"),  # 不满足（实际 semantic）
            FilterClause("tags", FilterOp.CONTAINS, "coffee"),
        ],
    )

    assert not matches_filters(unit, expr), "AND 任一支失败 → 整体拒绝"


def test_filter_or_needs_at_least_one(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["tea"])

    hit = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("tags", FilterOp.CONTAINS, "coffee"),
            FilterClause("tags", FilterOp.CONTAINS, "tea"),
        ],
    )
    miss = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("tags", FilterOp.CONTAINS, "coffee"),
            FilterClause("tags", FilterOp.CONTAINS, "milk"),
        ],
    )

    assert matches_filters(unit, hit), "OR 至少一支命中 → 通过"
    assert not matches_filters(unit, miss), "OR 全不命中 → 拒绝"


def test_filter_not_negates_child(unit_factory) -> None:
    unit = unit_factory("a", "x")  # tier=semantic

    assert matches_filters(
        unit, FilterGroup(FilterLogic.NOT, [FilterClause("tier", FilterOp.EQ, "core")])
    )
    assert not matches_filters(
        unit, FilterGroup(FilterLogic.NOT, [FilterClause("tier", FilterOp.EQ, "semantic")])
    )


def test_filter_numeric_predicate_on_native_number_metadata(unit_factory) -> None:
    # metadata 值是 JSON 标量原生类型：数值范围过滤直接生效，与后端下推同语义。
    unit = unit_factory("a", "x")
    unit.metadata["priority"] = 9

    assert matches_filters(unit, FilterClause("metadata.priority", FilterOp.GTE, 8))
    assert matches_filters(unit, FilterClause("metadata.priority", FilterOp.GTE, 9))
    assert not matches_filters(unit, FilterClause("metadata.priority", FilterOp.GT, 9))
    assert not matches_filters(unit, FilterClause("metadata.priority", FilterOp.LT, 8))


def test_filter_int_and_float_compare_across_units(unit_factory) -> None:
    # 同一 key 上 int 与 float 混存：真源复核按数值比较，不因写入形态分叉。
    # 对应 ES 侧 long→double 的 dynamic_template（首条整数不得把字段锁成整型）。
    as_int = unit_factory("a", "x")
    as_int.metadata["priority"] = 8
    as_float = unit_factory("b", "x")
    as_float.metadata["priority"] = 9.5

    gte_9 = FilterClause("metadata.priority", FilterOp.GTE, 9)
    assert not matches_filters(as_int, gte_9)
    assert matches_filters(as_float, gte_9)
    # 9.5 必须能被自己的精确值命中——被截断成 9 就查不出来
    assert matches_filters(as_float, FilterClause("metadata.priority", FilterOp.GTE, 9.5))


def test_filter_numeric_predicate_on_string_metadata_is_false_not_crash(unit_factory) -> None:
    # 类型不匹配（该 key 存的是字符串，谓词是数值）：判否而非抛 TypeError 中断整次
    # 检索。与 Milvus JSON 字段一致——类型严格、静默跳过，不做隐式转换。
    unit = unit_factory("a", "x")
    unit.metadata["priority"] = "high"

    assert not matches_filters(unit, FilterClause("metadata.priority", FilterOp.GTE, 8))
    assert not matches_filters(unit, FilterClause("metadata.priority", FilterOp.LT, 8))


def test_filter_range_op_on_set_field_is_false(unit_factory) -> None:
    """集合字段遇范围算子判否，不当作"不约束"放行。

    放行会让该谓词失效：图通道不下推 filters、只经本函数复核，一旦放行就直接错召。
    与标量不可比时的 TypeError 分支同答案。
    """
    unit = unit_factory("a", "x", tags=["work"])

    assert not matches_filters(unit, FilterClause("tags", FilterOp.GT, 5))
    assert not matches_filters(unit, FilterClause("tags", FilterOp.LTE, 5))
    # 集合上有意义的算子不受影响
    assert matches_filters(unit, FilterClause("tags", FilterOp.CONTAINS, "work"))


def test_filter_contains_rejects_scalar_even_on_exact_value(unit_factory) -> None:
    unit = unit_factory("a", "x")
    unit.metadata["project"] = "homework"

    assert not matches_filters(
        unit, FilterClause("metadata.project", FilterOp.CONTAINS, "homework")
    )
    assert not matches_filters(unit, FilterClause("metadata.project", FilterOp.CONTAINS, "work"))


def test_filter_scalar_ops_do_not_treat_array_as_scalar(unit_factory) -> None:
    unit = unit_factory("a", "x")
    unit.metadata["project"] = ["alpha", "beta"]

    assert matches_filters(unit, FilterClause("metadata.project", FilterOp.CONTAINS, "alpha"))
    assert not matches_filters(unit, FilterClause("metadata.project", FilterOp.EQ, "alpha"))
    assert matches_filters(unit, FilterClause("metadata.project", FilterOp.NE, "alpha"))
    assert not matches_filters(
        unit, FilterClause("metadata.project", FilterOp.IN, ["alpha", "gamma"])
    )
    assert matches_filters(
        unit, FilterClause("metadata.project", FilterOp.NOT_IN, ["alpha", "gamma"])
    )


def test_current_query_excludes_expired_active(unit_factory) -> None:
    """t_invalid 已过但 lifecycle 仍是 ACTIVE 的记忆不属于"当前有效"。

    lifecycle 与 valid-time 是两套独立的失效机制。状态清扫尚未执行时，这类中间态
    仍不能被当前态查询当作有效。
    """
    expired = unit_factory("a", "x", t_invalid=datetime.now(timezone.utc) - timedelta(days=1))

    assert expired.lifecycle == LifecycleState.ACTIVE
    assert not passes(expired, None)


def test_current_query_excludes_not_yet_valid(unit_factory) -> None:
    """t_valid 尚未到达的记忆同样不属于"当前有效"（经 MemoryPatch.t_valid 可设未来）。"""
    future = unit_factory("a", "x", t_valid=datetime.now(timezone.utc) + timedelta(days=1))

    assert not passes(future, None)


def test_current_query_keeps_active_within_valid_window(unit_factory) -> None:
    """常规活跃记忆不受影响：t_valid 已过、t_invalid 为空或未到。"""
    now = datetime.now(timezone.utc)
    open_ended = unit_factory("a", "x", t_valid=now - timedelta(days=1))
    later = unit_factory("b", "x", t_valid=now - timedelta(days=1), t_invalid=now + timedelta(1))

    assert passes(open_ended, None)
    assert passes(later, None)


def test_explicit_t_invalid_filter_sees_sentinel_not_none(unit_factory) -> None:
    """显式 t_invalid 谓词在复核处看到哨兵，与索引投影同值。

    返回 None 会让缺值分支砍掉一切正向谓词：下推按哨兵召回、复核按缺值砍掉，
    调用方查"仍有效的"（t_invalid > now）反而拿到空集。
    """
    unit = unit_factory("a", "x")
    assert unit.temporal.t_invalid is None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert matches_filters(unit, FilterClause("t_invalid", FilterOp.GT, now_ms))
    assert matches_filters(unit, FilterClause("t_invalid", FilterOp.EQ, T_INVALID_OPEN))
    assert not matches_filters(unit, FilterClause("t_invalid", FilterOp.LT, now_ms))


def test_valid_at_reads_source_of_truth_not_sentinel(unit_factory) -> None:
    """valid_at 不受哨兵影响——它直接读真源 None，判"永久有效"。"""
    unit = unit_factory("a", "x")

    assert valid_at(unit, datetime(9999, 1, 1, tzinfo=timezone.utc))


def test_valid_at_open_ended_interval_is_valid(unit_factory) -> None:
    """t_invalid 为空 = 永久有效，回溯查询必须能命中。

    索引侧将空值投影为 T_INVALID_OPEN，历史查询可以安全下推 ``t_invalid > as_of``；
    真源侧仍保留 None，valid_at 按开放区间判定。
    """
    unit = unit_factory("a", "x", t_valid=NOW - timedelta(days=1))

    assert unit.temporal.t_invalid is None
    assert passes(unit, NOW)
    assert passes(unit, NOW + timedelta(days=365))


def test_filter_boolean_metadata(unit_factory) -> None:
    # JSON bool 原生带入，等值过滤直接生效
    unit = unit_factory("a", "x")
    unit.metadata["archived"] = False

    assert matches_filters(unit, FilterClause("metadata.archived", FilterOp.EQ, False))
    assert not matches_filters(unit, FilterClause("metadata.archived", FilterOp.EQ, True))


def test_filter_numeric_predicate_on_system_field_works(unit_factory) -> None:
    # 系统时间字段在真源侧是 epoch 毫秒 int，数值范围过滤照常生效
    unit = unit_factory("a", "x", t_event=NOW)
    epoch_ms = int(NOW.timestamp() * 1000)

    assert matches_filters(unit, FilterClause("t_event", FilterOp.GTE, epoch_ms))
    assert not matches_filters(unit, FilterClause("t_event", FilterOp.GT, epoch_ms))


def test_filter_none_passes_all(unit_factory) -> None:
    assert matches_filters(unit_factory("a", "x"), None), "None → 无过滤，全通过"


def test_filter_metadata_memory_type_uses_canonical_field(unit_factory) -> None:
    unit = unit_factory("a", "x")
    unit.metadata["memory_type"] = "coding"

    assert matches_filters(
        unit, normalize(FilterClause("metadata.memory_type", FilterOp.EQ, "coding"))
    )
    assert matches_filters(unit, normalize(FilterClause("memory_type", FilterOp.EQ, "coding")))


def test_filter_flat_list_normalized_is_and(unit_factory) -> None:
    # 旧式扁平 list 经 normalize 收口为 AND 树，内核只见 FilterExpr
    unit = unit_factory("a", "x", tags=["coffee"])
    flat = [
        FilterClause("tags", FilterOp.CONTAINS, "coffee"),
        FilterClause("tier", FilterOp.EQ, "semantic"),
    ]

    assert matches_filters(unit, normalize(flat)) is True
    assert matches_filters(unit, normalize([FilterClause("tier", FilterOp.EQ, "core")])) is False


# -- scope 内点读（load） ------------------------------------------------------ #


def test_unit_reader_loads_by_id_in_scope(scope, unit_factory) -> None:
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(unit_factory("u1", "hello")))
    reader = UnitReader(kv)

    loaded = reader.load(scope, ["u1", "missing"])

    assert set(loaded) == {"u1"}
    assert loaded["u1"].content == "hello"


def test_unit_reader_load_batch_omits_missing(scope, unit_factory) -> None:
    # load 一次性 mget 召回：批量命中省逐条 get 往返，缺失 id 省略。
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(unit_factory("u1", "one")))
    kv.insert(scope, memory_key("u2"), dumps(unit_factory("u2", "two")))
    reader = UnitReader(kv)

    loaded = reader.load(scope, ["u1", "u2", "missing"])

    assert set(loaded) == {"u1", "u2"}
    assert loaded["u1"].content == "one"
    assert loaded["u2"].content == "two"


def test_unit_reader_load_dedups_repeated_ids(scope, unit_factory) -> None:
    # 重复 uid 的去重留在 load（不下沉到 mget）：同一 uid 传多次只点读一次。
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(unit_factory("u1", "x")))
    reader = UnitReader(kv)

    loaded = reader.load(scope, ["u1", "u1", "u1"])

    assert set(loaded) == {"u1"}
    assert loaded["u1"].content == "x"


def test_unit_reader_load_empty_ids_returns_empty(scope) -> None:
    assert UnitReader(InMemoryKVStore()).load(scope, []) == {}


def test_unit_reader_load_all_missing_returns_empty(scope) -> None:
    assert UnitReader(InMemoryKVStore()).load(scope, ["ghost"]) == {}
