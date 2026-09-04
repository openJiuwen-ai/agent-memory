"""过滤表达式（FilterClause/FilterGroup/FilterExpr）规范化·校验·求值测试。"""

from __future__ import annotations

from typing import Callable

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    and_merge,
    evaluate,
    extract_required_equality,
    from_dict,
    iter_clauses,
    normalize,
    validate,
)

pytestmark = pytest.mark.unit


def _eq_leaf(state: dict) -> Callable[[FilterClause], bool]:
    """按 EQ 语义对 state 求值的叶子谓词（够本文件用例）。"""
    return lambda c: state.get(c.field) == c.value


# -- normalize ---------------------------------------------------------------- #


def test_normalize_list_becomes_and_group() -> None:
    c1 = FilterClause("project", FilterOp.EQ, "alpha")
    c2 = FilterClause("priority", FilterOp.GTE, 8)

    expr = normalize([c1, c2])

    assert isinstance(expr, FilterGroup), "list 应规范化为 FilterGroup"
    assert expr.logic is FilterLogic.AND, "多子句默认 AND 组合"
    assert expr.children == [
        FilterClause("user_metadata.project", FilterOp.EQ, "alpha"),
        FilterClause("user_metadata.priority", FilterOp.GTE, 8),
    ]


def test_normalize_single_clause_preserves_semantics() -> None:
    c = FilterClause("project", FilterOp.EQ, "alpha")

    assert normalize(c) == FilterClause("user_metadata.project", FilterOp.EQ, "alpha")


def test_normalize_none_and_empty_list_are_none() -> None:
    assert normalize(None) is None, "None → None（无过滤）"
    assert normalize([]) is None, "空 list → None（无过滤）"


def test_normalize_accepts_dict_dsl() -> None:
    assert normalize({"project": "alpha"}) == FilterClause(
        "user_metadata.project", FilterOp.EQ, "alpha"
    )


def test_normalize_rejects_non_clause_list_element() -> None:
    with pytest.raises(ValidationError):
        normalize([FilterClause("p", FilterOp.EQ, 1), "not-a-clause"])


# -- validate ----------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    ["scope", "tenant", "org", "space", "user", "agent", "session", "scope_x"],
)
def test_validate_rejects_scope_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause(field, FilterOp.EQ, "v"))


def test_validate_rejects_empty_field_name() -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause("", FilterOp.EQ, "v"))


def test_validate_rejects_empty_and_or_group() -> None:
    for logic in (FilterLogic.AND, FilterLogic.OR):
        with pytest.raises(ValidationError):
            validate(FilterGroup(logic, []))


def test_validate_not_requires_exactly_one_child() -> None:
    one = FilterClause("s", FilterOp.EQ, "x")
    validate(FilterGroup(FilterLogic.NOT, [one]))  # ok
    with pytest.raises(ValidationError):
        validate(FilterGroup(FilterLogic.NOT, [one, one]))


def test_validate_recurses_into_nested_scope_field() -> None:
    nested = FilterGroup(
        FilterLogic.AND, [FilterGroup(FilterLogic.OR, [FilterClause("user", FilterOp.EQ, "u")])]
    )
    with pytest.raises(ValidationError):
        validate(nested)


# -- iter_clauses ------------------------------------------------------------- #


def test_iter_clauses_yields_all_leaves_depth_first() -> None:
    a, b, c = (FilterClause("a", FilterOp.EQ, 1), FilterClause("b", FilterOp.EQ, 2),
               FilterClause("c", FilterOp.EQ, 3))
    tree = FilterGroup(FilterLogic.AND, [a, FilterGroup(FilterLogic.OR, [b, c])])

    assert list(iter_clauses(tree)) == [a, b, c]
    assert list(iter_clauses(None)) == []
    assert list(iter_clauses(a)) == [a]


# -- evaluate ----------------------------------------------------------------- #


def test_evaluate_none_is_true() -> None:
    assert evaluate(None, _eq_leaf({})) is True, "无过滤 → 全通过"


def test_evaluate_and_or_not_semantics() -> None:
    tree = FilterGroup(FilterLogic.AND, [
        FilterClause("p", FilterOp.EQ, "alpha"),
        FilterGroup(
            FilterLogic.OR,
            [FilterClause("a", FilterOp.EQ, "x"), FilterClause("a", FilterOp.EQ, "y")],
        ),
        FilterGroup(FilterLogic.NOT, [FilterClause("s", FilterOp.EQ, "archived")]),
    ])

    assert evaluate(tree, _eq_leaf({"p": "alpha", "a": "y", "s": "active"})) is True
    assert evaluate(tree, _eq_leaf({"p": "beta", "a": "y", "s": "active"})) is False, "AND 一支失败"
    assert evaluate(tree, _eq_leaf({"p": "alpha", "a": "z", "s": "active"})) is False, "OR 无命中"
    assert (
        evaluate(tree, _eq_leaf({"p": "alpha", "a": "x", "s": "archived"})) is False
    ), "NOT 命中被取反"


# -- and_merge：系统谓词防稀释（§3 风险点） ----------------------------------- #


def test_and_merge_wraps_user_expr_as_whole_child() -> None:
    user = FilterGroup(
        FilterLogic.OR, [FilterClause("p", FilterOp.EQ, "a"), FilterClause("p", FilterOp.EQ, "b")]
    )
    sysf = [FilterClause("lifecycle", FilterOp.EQ, "active")]

    merged = and_merge(user, sysf)

    assert isinstance(merged, FilterGroup) and merged.logic is FilterLogic.AND
    assert merged.children[0] is sysf[0], "系统谓词在外层 AND"
    assert merged.children[-1] is user, "用户表达式作为整体 child，未摊平"


def test_and_merge_does_not_let_user_or_dilute_system_predicate() -> None:
    # 用户 OR 命中、但 lifecycle 非 active 的记录必须被系统谓词拦下（不被 OR 稀释）
    user = FilterGroup(
        FilterLogic.OR, [FilterClause("p", FilterOp.EQ, "a"), FilterClause("p", FilterOp.EQ, "b")]
    )
    merged = and_merge(user, [FilterClause("lifecycle", FilterOp.EQ, "active")])

    assert evaluate(merged, _eq_leaf({"p": "a", "lifecycle": "superseded"})) is False, \
        "OR 命中但生命周期失效 → 整体应拒绝"
    assert evaluate(merged, _eq_leaf({"p": "a", "lifecycle": "active"})) is True


def test_and_merge_degenerate_cases() -> None:
    c = FilterClause("lifecycle", FilterOp.EQ, "active")
    assert and_merge(None, [c]) is c, "仅单条系统谓词、无用户表达式 → 直接返回该谓词"
    assert and_merge(None, []) is None, "无任何谓词 → None"
    user = FilterClause("p", FilterOp.EQ, "a")
    assert and_merge(user, []) is user, "仅用户表达式、无系统谓词 → 原样返回"


# -- validate 加固：非法 logic/op/value ---------------------------------------- #


def test_validate_rejects_non_enum_logic() -> None:
    with pytest.raises(ValidationError):
        validate(FilterGroup("xor", [FilterClause("a", FilterOp.EQ, 1)]))


def test_validate_rejects_non_enum_op() -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause("a", "bogus", 1))


def test_validate_rejects_in_with_non_collection() -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause("a", FilterOp.IN, "not-a-list"))  # 字符串不是集合
    with pytest.raises(ValidationError):
        validate(FilterClause("a", FilterOp.IN, 5))
    with pytest.raises(ValidationError):
        validate(FilterClause("a", FilterOp.IN, {"x", "y"}))
    with pytest.raises(ValidationError):
        validate(FilterClause("a", FilterOp.IN, []))
    with pytest.raises(ValidationError):
        validate(FilterClause("a", FilterOp.IN, ["x", 1]))
    validate(FilterClause("a", FilterOp.IN, ["x", "y"]))


def test_normalize_tuple_values_are_canonical_lists() -> None:
    expr = normalize(FilterClause("a", FilterOp.IN, ("x", "y")))

    assert isinstance(expr, FilterClause)
    assert expr.value == ["x", "y"]


def test_validate_accepts_mixed_int_float_as_one_numeric_kind() -> None:
    expr = normalize(FilterClause("score", FilterOp.IN, [1, 1.5]))

    assert isinstance(expr, FilterClause)
    assert expr.value == [1, 1.5]


def test_validate_rejects_range_with_non_scalar() -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause("n", FilterOp.GTE, [1, 2]))
    with pytest.raises(ValidationError):
        validate(FilterClause("n", FilterOp.GT, None))
    with pytest.raises(ValidationError):
        validate(FilterClause("n", FilterOp.GT, "8"))
    with pytest.raises(ValidationError):
        validate(FilterClause("n", FilterOp.GT, True))
    with pytest.raises(ValidationError):
        validate(FilterClause("n", FilterOp.GT, float("inf")))
    validate(FilterClause("n", FilterOp.GTE, 8))


@pytest.mark.parametrize(
    "op,value",
    [
        (FilterOp.EQ, None),
        (FilterOp.EQ, ["x"]),
        (FilterOp.NE, {"x": 1}),
        (FilterOp.CONTAINS, ["x"]),
        (FilterOp.CONTAINS, None),
    ],
)
def test_validate_rejects_non_scalar_eq_ne_contains(op: FilterOp, value) -> None:
    with pytest.raises(ValidationError):
        validate(FilterClause("a", op, value))


@pytest.mark.parametrize("value", ["x", 1, 1.5, True])
def test_validate_accepts_serializable_scalar_values(value) -> None:
    validate(FilterClause("a", FilterOp.EQ, value))


def test_evaluate_unknown_logic_raises_not_treated_as_not() -> None:
    # 直接构造非法 logic（绕过 validate），evaluate 必须报错而非当成 NOT
    bad = FilterGroup("xor", [FilterClause("a", FilterOp.EQ, 1)])
    with pytest.raises(ValidationError):
        evaluate(bad, lambda c: True)


# -- dict DSL：from_dict / normalize(dict) ------------------------------------ #


def test_from_dict_field_eq_and_op() -> None:
    assert from_dict({"user_metadata.project": "alpha"}) == FilterClause(
        "user_metadata.project", FilterOp.EQ, "alpha"
    )
    assert from_dict({"user_metadata.priority": {"gte": 8}}) == FilterClause(
        "user_metadata.priority", FilterOp.GTE, 8
    )


def test_memory_type_is_canonicalized_to_metadata_field() -> None:
    assert from_dict({"memory_type": "coding"}) == FilterClause(
        "system_metadata.memory_type", FilterOp.EQ, "coding"
    )
    assert normalize(FilterClause("memory_type", FilterOp.EQ, "coding")) == FilterClause(
        "system_metadata.memory_type", FilterOp.EQ, "coding"
    )


def test_memory_type_aliases_share_one_required_equality_field() -> None:
    same = normalize(
        {
            "OR": [
                {"memory_type": "coding"},
                {"system_metadata.memory_type": "coding"},
            ]
        }
    )
    conflict = normalize(
        {
            "AND": [
                {"memory_type": "coding"},
                {"system_metadata.memory_type": "general"},
            ]
        }
    )

    assert extract_required_equality(same, "system_metadata.memory_type") == "coding"
    assert extract_required_equality(conflict, "system_metadata.memory_type") is None


def test_from_dict_logic_tree() -> None:
    d = {
        "AND": [
            {"project": {"in": ["a", "b"]}},
            {"OR": [{"x": 1}, {"x": 2}]},
            {"NOT": {"s": "archived"}},
        ]
    }
    expr = from_dict(d)

    assert isinstance(expr, FilterGroup) and expr.logic is FilterLogic.AND
    assert expr.children[1].logic is FilterLogic.OR
    assert expr.children[2].logic is FilterLogic.NOT


def test_from_dict_top_level_multi_key_is_and() -> None:
    expr = from_dict({"a": 1, "b": 2})

    assert isinstance(expr, FilterGroup) and expr.logic is FilterLogic.AND
    assert len(expr.children) == 2


def test_normalize_dict_validates_and_rejects_bad_dsl() -> None:
    assert normalize({}) is None
    assert normalize({"project": {"in": ["a", "b"]}}) == FilterClause(
        "user_metadata.project", FilterOp.IN, ["a", "b"]
    )
    with pytest.raises(ValidationError):
        normalize({"AND": []})  # 空列表
    with pytest.raises(ValidationError):
        normalize({"x": {"bogus": 1}})  # 未知算子
    with pytest.raises(ValidationError):
        normalize({"user": "u"})  # scope 字段禁入


# -- extract_required_equality：语义化路由 ----------------------------------- #


def test_required_equality_direct_and_and() -> None:
    assert extract_required_equality(FilterClause("mt", FilterOp.EQ, "coding"), "mt") == "coding"
    tree = FilterGroup(
        FilterLogic.AND,
        [FilterClause("mt", FilterOp.EQ, "coding"), FilterClause("p", FilterOp.EQ, "x")],
    )
    assert extract_required_equality(tree, "mt") == "coding"


def test_required_equality_none_for_or_not_and_conflict() -> None:
    or_diff = FilterGroup(
        FilterLogic.OR,
        [FilterClause("mt", FilterOp.EQ, "coding"), FilterClause("mt", FilterOp.EQ, "general")],
    )
    assert extract_required_equality(or_diff, "mt") is None, "OR 多值 → 不确定"

    not_eq = FilterGroup(FilterLogic.NOT, [FilterClause("mt", FilterOp.EQ, "coding")])
    assert extract_required_equality(not_eq, "mt") is None, "NOT 下等值 → 不确定"

    conflict = FilterGroup(
        FilterLogic.AND,
        [FilterClause("mt", FilterOp.EQ, "a"), FilterClause("mt", FilterOp.EQ, "b")],
    )
    assert extract_required_equality(conflict, "mt") is None, "AND 冲突值 → 不确定"


def test_required_equality_or_same_value() -> None:
    same = FilterGroup(
        FilterLogic.OR,
        [FilterClause("mt", FilterOp.EQ, "coding"), FilterClause("mt", FilterOp.EQ, "coding")],
    )
    assert extract_required_equality(same, "mt") == "coding", "OR 全分支同值 → 确定"
    assert extract_required_equality(same, "other") is None
