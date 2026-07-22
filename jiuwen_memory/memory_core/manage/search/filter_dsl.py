# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compatibility shim — re-exports the canonical FilterGroup DSL.

The DSL implementation lives at
``jiuwen_memory.foundation.store.filter_dsl`` so that the
foundation/store layer can reference it without triggering a circular
import through ``memory_core``. This module preserves the public import
path that callers were instructed to use.
"""
from jiuwen_memory.foundation.store.filter_dsl import (  # noqa: F401
    FilterCondition,
    FilterGroup,
    FilterLogic,
    FilterOperator,
    MAX_NESTING_DEPTH,
)

__all__ = [
    "FilterCondition",
    "FilterGroup",
    "FilterLogic",
    "FilterOperator",
    "MAX_NESTING_DEPTH",
]
