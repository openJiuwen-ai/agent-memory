# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for normalize_filters and ensure_blacklisted_ne."""
import pytest

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterLogic,
    FilterOperator,
)
from jiuwen_memory.memory_core.manage.search.filter_normalizer import (
    ensure_blacklisted_ne,
    normalize_filters,
    _has_field,
)


class TestNormalizeFilters:
    @staticmethod
    def test_none_returns_none():
        assert normalize_filters(None) is None

    @staticmethod
    def test_filter_group_returned_as_is():
        g = FilterGroup(conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
        ])
        norm = normalize_filters(g)
        assert norm is not None
        assert isinstance(norm, FilterGroup)
        assert len(norm.conditions) == 1

    @staticmethod
    def test_dict_rejected():
        with pytest.raises(BaseError) as exc:
            normalize_filters({"a": 1})  # type: ignore[arg-type]
        assert exc.value.status == StatusCode.MEMORY_FILTER_FORMAT_ERROR

    @staticmethod
    def test_other_types_rejected():
        with pytest.raises(BaseError):
            normalize_filters(123)  # type: ignore[arg-type]
        with pytest.raises(BaseError):
            normalize_filters("filters")  # type: ignore[arg-type]


class TestEnsureBlacklistedNe:
    @staticmethod
    def test_none_injects_default():
        g = ensure_blacklisted_ne(None)
        assert len(g.conditions) == 1
        c = g.conditions[0]
        assert isinstance(c, FilterCondition)
        assert c.field == "blacklisted"
        assert c.op == FilterOperator.NE
        assert c.value is True

    @staticmethod
    def test_group_without_blacklisted_gets_injected():
        original = FilterGroup(conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
        ])
        g = ensure_blacklisted_ne(original)
        assert len(g.conditions) == 2
        # The original group is preserved as the first (nested) condition.
        assert g.conditions[0] is original
        # The injected NE(blacklisted, True) is the second condition.
        injected = g.conditions[1]
        assert isinstance(injected, FilterCondition)
        assert injected.field == "blacklisted"
        assert injected.op == FilterOperator.NE
        assert injected.value is True
        # And the original "a" condition still exists in the tree.
        assert _has_field(g, "a")

    @staticmethod
    def test_group_with_explicit_blacklisted_left_untouched():
        original = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True),
        ])
        g = ensure_blacklisted_ne(original)
        assert g is original
        assert len(g.conditions) == 1

    @staticmethod
    def test_nested_group_with_blacklisted_left_untouched():
        inner = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", op=FilterOperator.NE, value=True),
        ])
        outer = FilterGroup(conditions=[
            inner,
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
        ])
        g = ensure_blacklisted_ne(outer)
        assert g is outer


class TestHasField:
    @staticmethod
    def test_top_level_match():
        g = FilterGroup(conditions=[
            FilterCondition(field="a", value=1),
            FilterCondition(field="b", value=2),
        ])
        assert _has_field(g, "b")
        assert not _has_field(g, "c")

    @staticmethod
    def test_nested_match():
        inner = FilterGroup(conditions=[
            FilterCondition(field="blacklisted", value=True),
        ])
        outer = FilterGroup(conditions=[inner, FilterCondition(field="a", value=1)])
        assert _has_field(outer, "blacklisted")

    @staticmethod
    def test_no_match():
        g = FilterGroup(conditions=[FilterCondition(field="a", value=1)])
        assert not _has_field(g, "blacklisted")
