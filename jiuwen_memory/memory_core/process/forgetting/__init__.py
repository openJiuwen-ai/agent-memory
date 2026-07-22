# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Ebbinghaus-style memory forgetting — evaluator abstraction + built-in implementation.

The forget step is orchestrated by ``LongTermMemory._forget_memories_step``.
The orchestrator is responsible for: lock acquisition, sweeping candidate
memories via ``list_memories``, delegating the scoring / filter decision
to a ``ForgetEvaluator``, and applying the resulting blacklist updates.

The orchestrator is intentionally agnostic of the scoring algorithm. Any
subclass of ``ForgetEvaluator`` plugs in via ``ForgettingConfig.evaluator``
— built-in ``EbbinghausEvaluator`` is the default when ``evaluator is None``.

Dependency injection contract:
    - Cross-cutting dependencies (``BaseKVStore`` / SQL engine / external
      API clients) are injected via the evaluator's ``__init__``.
    - Per-invocation state (``scope_id`` / ``user_id`` / ``now`` / config)
      flows through ``ForgetContext``.
This split keeps the ``evaluate`` signature stable: new data sources are
absorbed by injecting them into a custom subclass, not by adding fields
to ``ForgetContext``.
"""

from jiuwen_memory.memory_core.process.forgetting.evaluator import (
    EbbinghausEvaluator,
    ForgetContext,
    ForgetEvaluator,
    TYPE_WEIGHT,
    SIGMA,
    parse_iso,
)

__all__ = [
    "EbbinghausEvaluator",
    "ForgetContext",
    "ForgetEvaluator",
    "TYPE_WEIGHT",
    "SIGMA",
    "parse_iso",
]
