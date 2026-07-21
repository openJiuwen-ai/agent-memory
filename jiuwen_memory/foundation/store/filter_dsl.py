# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FilterGroup DSL — backend-neutral scalar filter expression model.

Scope of the first version:
  - operators: EQ / NE only (GT/GTE/LT/LTE/IN/NIN/contains are out of scope)
  - value: scalar only (str / int / float / bool / None); list/dict rejected
  - logic: AND / OR with arbitrary nesting (depth capped at MAX_NESTING_DEPTH)

The DSL is rendered to backend-native syntax by each vector store's
``_render_filters`` implementation. See ``filter_normalizer`` for the
single-entry validation pipeline and ``ensure_blacklisted_ne`` for the
default filter injection used by search / list entrypoints.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, Field, model_validator


class FilterOperator(str, Enum):
    """Supported scalar operators for FilterCondition."""

    EQ = "eq"  # ==
    NE = "ne"  # !=
    # First version deliberately excludes GT/GTE/LT/LTE/IN/NIN/contains/like/regex.


class FilterLogic(str, Enum):
    """Logical combiner for FilterGroup.conditions."""

    AND = "and"
    OR = "or"


# Hard cap on FilterGroup nesting depth. Renderers must refuse anything deeper.
MAX_NESTING_DEPTH = 16


class FilterCondition(BaseModel):
    """A single scalar condition: ``field <op> value``.

    For EQ / NE, ``value`` must be a scalar (str / int / float / bool / None).
    List / dict values are rejected at validation time.
    """

    field: str
    op: FilterOperator = FilterOperator.EQ
    value: Any

    @model_validator(mode="after")
    def _check_value_scalar(self) -> "FilterCondition":
        if self.op in (FilterOperator.EQ, FilterOperator.NE):
            if isinstance(self.value, (list, dict)):
                raise ValueError(
                    f"{self.op} value must be scalar, got {type(self.value).__name__}"
                )
        return self

    @model_validator(mode="after")
    def _check_field_non_empty(self) -> "FilterCondition":
        if not self.field or not str(self.field).strip():
            raise ValueError("FilterCondition.field must be non-empty")
        return self


class FilterGroup(BaseModel):
    """A logical group of conditions and/or nested groups.

    Arbitrary nesting is allowed but capped at ``MAX_NESTING_DEPTH``;
    deeper groups are rejected by the depth-checking validator.
    """

    logic: FilterLogic = FilterLogic.AND
    conditions: list[Union["FilterCondition", "FilterGroup"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_depth(self) -> "FilterGroup":
        self._validate_depth(0)
        return self

    def _validate_depth(self, depth: int) -> None:
        if depth > MAX_NESTING_DEPTH:
            raise ValueError(
                f"FilterGroup nesting depth exceeds {MAX_NESTING_DEPTH}"
            )
        for c in self.conditions:
            if isinstance(c, FilterGroup):
                # Same-class access to a protected method; recursive depth
                # validation cannot be lifted to a public API.
                c._validate_depth(depth + 1)  # pylint: disable=protected-access


FilterGroup.model_rebuild()


__all__ = [
    "FilterOperator",
    "FilterLogic",
    "FilterCondition",
    "FilterGroup",
    "MAX_NESTING_DEPTH",
]
