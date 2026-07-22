# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Normalization + default-injection entry for the FilterGroup DSL.

All search / list entrypoints MUST route filters through ``normalize_filters``
before forwarding to managers / vector stores, so that backends receive a
validated FilterGroup (or None). ``ensure_blacklisted_ne`` is the convenience
helper used by search / list to auto-inject ``NE("blacklisted", True)`` when
the caller did not provide it explicitly.
"""
from __future__ import annotations

from typing import Optional

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterOperator,
)


def normalize_filters(filters: Optional[FilterGroup]) -> Optional[FilterGroup]:
    """Validate / normalize caller-supplied filters.

    Rules:
      - None → None (caller explicitly opts out of filtering)
      - FilterGroup → re-run pydantic validators and return as-is
      - dict is NOT accepted (callers must construct FilterGroup explicitly);
        raise MEMORY_FILTER_FORMAT_ERROR
      - anything else → raise MEMORY_FILTER_FORMAT_ERROR

    Args:
        filters: raw caller-supplied filters (None / FilterGroup / dict / other)

    Returns:
        Optional[FilterGroup]: normalized value to forward to backends.

    Raises:
        BaseError(StatusCode.MEMORY_FILTER_FORMAT_ERROR): on type / validation errors.
    """
    if filters is None:
        return None
    if isinstance(filters, FilterGroup):
        # Re-run model_validate to trigger nested validators (depth, scalar value).
        return FilterGroup.model_validate(filters.model_dump())
    if isinstance(filters, dict):
        raise build_error(
            StatusCode.MEMORY_FILTER_FORMAT_ERROR,
            error_msg="filters must be a FilterGroup instance, dict is not accepted",
        )
    raise build_error(
        StatusCode.MEMORY_FILTER_FORMAT_ERROR,
        error_msg=f"filters must be FilterGroup or None, got {type(filters).__name__}",
    )


def _has_field(group: FilterGroup, field_name: str) -> bool:
    """Recursively check whether any FilterCondition in group targets ``field_name``."""
    for c in group.conditions:
        if isinstance(c, FilterCondition) and c.field == field_name:
            return True
        if isinstance(c, FilterGroup) and _has_field(c, field_name):
            return True
    return False


def ensure_blacklisted_ne(filters: Optional[FilterGroup]) -> FilterGroup:
    """Inject ``NE("blacklisted", True)`` unless the caller already targets that field.

    Used by search / list entrypoints so callers do not have to repeat the
    "exclude forgotten memories" clause every time.

    Args:
        filters: caller-supplied filters (may be None or a FilterGroup).

    Returns:
        FilterGroup: a group whose top-level conditions include
        ``NE("blacklisted", True)``.
    """
    ne_blacklisted = FilterCondition(
        field="blacklisted", op=FilterOperator.NE, value=True
    )
    if filters is None:
        return FilterGroup(conditions=[ne_blacklisted])
    if _has_field(filters, "blacklisted"):
        return filters
    return FilterGroup(
        conditions=[filters, ne_blacklisted],
    )


__all__ = ["normalize_filters", "ensure_blacklisted_ne"]
