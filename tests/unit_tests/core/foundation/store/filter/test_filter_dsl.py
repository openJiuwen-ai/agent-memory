# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for FilterGroup DSL validation rules (§3.16 test matrix)."""
import pytest
from pydantic import ValidationError

from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterLogic,
    FilterOperator,
    MAX_NESTING_DEPTH,
)


class TestFilterConditionValidation:
    @staticmethod
    def test_eq_default_operator():
        c = FilterCondition(field="category", value="work")
        assert c.op == FilterOperator.EQ
        assert c.value == "work"

    @staticmethod
    def test_eq_accepts_scalar_types():
        for v in ("work", 1, 1.5, True, None):
            c = FilterCondition(field="f", op=FilterOperator.EQ, value=v)
            assert c.value == v

    @staticmethod
    def test_eq_rejects_list_value():
        with pytest.raises(ValidationError):
            FilterCondition(field="f", op=FilterOperator.EQ, value=[1, 2])

    @staticmethod
    def test_eq_rejects_dict_value():
        with pytest.raises(ValidationError):
            FilterCondition(field="f", op=FilterOperator.EQ, value={"a": 1})

    @staticmethod
    def test_ne_accepts_scalar():
        c = FilterCondition(field="f", op=FilterOperator.NE, value=False)
        assert c.value is False

    @staticmethod
    def test_ne_rejects_list():
        with pytest.raises(ValidationError):
            FilterCondition(field="f", op=FilterOperator.NE, value=[1])

    @staticmethod
    def test_field_must_be_non_empty():
        with pytest.raises(ValidationError):
            FilterCondition(field="   ", value=1)

    @staticmethod
    def test_field_must_be_present():
        with pytest.raises(ValidationError):
            FilterCondition(value=1)  # type: ignore[call-arg]


class TestFilterGroupNesting:
    @staticmethod
    def test_and_group_renders():
        g = FilterGroup(conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="b", op=FilterOperator.NE, value=2),
        ])
        assert g.logic == FilterLogic.AND
        assert len(g.conditions) == 2

    @staticmethod
    def test_or_group():
        g = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="a", op=FilterOperator.EQ, value=2),
        ])
        assert g.logic == FilterLogic.OR

    @staticmethod
    def test_nested_group():
        inner = FilterGroup(logic=FilterLogic.OR, conditions=[
            FilterCondition(field="a", op=FilterOperator.EQ, value=1),
            FilterCondition(field="a", op=FilterOperator.EQ, value=2),
        ])
        outer = FilterGroup(conditions=[
            inner,
            FilterCondition(field="b", op=FilterOperator.EQ, value="x"),
        ])
        assert isinstance(outer.conditions[0], FilterGroup)

    @staticmethod
    def test_nested_depth_limit_enforced():
        # A group nested exactly MAX_NESTING_DEPTH layers deep must construct successfully,
        # but one more layer of nesting must be rejected by the depth-check validator.
        g = FilterGroup(conditions=[FilterCondition(field="f", value=1)])
        for _ in range(MAX_NESTING_DEPTH):
            g = FilterGroup(conditions=[g])  # depth capped at MAX_NESTING_DEPTH
        # One more layer should be rejected at construction time.
        with pytest.raises(ValidationError):
            FilterGroup(conditions=[g])

    @staticmethod
    def test_nested_depth_limit_enforced_via_model_validate():
        # Even when bypassing the constructor (raw dict), the validator must still
        # reject groups deeper than MAX_NESTING_DEPTH.
        raw = {
            "conditions": [
                FilterCondition(field="f", value=1).model_dump()
            ]
        }
        for _ in range(MAX_NESTING_DEPTH + 1):
            raw = {"conditions": [raw]}
        with pytest.raises(ValidationError):
            FilterGroup.model_validate(raw)
