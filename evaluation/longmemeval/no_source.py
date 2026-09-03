"""Evaluation-scoped ``retain_source=false`` compatibility layer.

This is the GitCode 91.8 no-source contract, isolated to the LongMemEval
evaluation process. It replaces only the non-procedural EXTRACT path while a
sample is ingested and restores the original method in ``finally``.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Any, Iterator

from evaluation.longmemeval.adapter import LONG_TURN_CONTEXT_KEY


@contextmanager
def no_source_extraction() -> Iterator[None]:
    from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
        OrchestratingEvolver,
    )
    from jiuwen_memory.construction.prompt_strategy import copy_consolidation_prompts

    original = OrchestratingEvolver._evolve_extract
    parameters = tuple(inspect.signature(original).parameters)
    if parameters != ("self", "units"):
        raise RuntimeError(
            "OrchestratingEvolver._evolve_extract interface changed: "
            f"expected (self, units), got {parameters}"
        )

    def _evolve_extract_without_source(self: Any, units: list[Any]):
        if self._is_procedural(units):
            return original(self, units)

        context_texts = [
            str(unit.system_metadata.pop(LONG_TURN_CONTEXT_KEY, "")).strip()
            for unit in units
        ]
        from jiuwen_memory.common.type_def import MemoryUnit, Segment
        from jiuwen_memory.construction.base import ExtractContext

        recent_originals = [
            MemoryUnit(
                id=f"{unit.id}-sentence-context",
                scope=unit.scope,
                segments=[Segment(content=text, source=unit.source)],
                temporal=unit.temporal,
            )
            for unit, text in zip(units, context_texts)
            if text
        ]
        context = (
            ExtractContext(recent_originals=recent_originals)
            if recent_originals
            else None
        )
        extracted = self._extractor.extract(units, context=context)
        if not extracted:
            from jiuwen_memory.construction.evolver import EvolveResult

            return EvolveResult()
        copy_consolidation_prompts(units, extracted)
        for unit in extracted:
            unit.system_metadata.pop("evidence", None)
            unit.source_ref = None
            unit.system_metadata["source_retained"] = "false"
        self._annotate_layers(extracted)
        return self._dedup_batch(extracted)

    OrchestratingEvolver._evolve_extract = _evolve_extract_without_source
    try:
        yield
    finally:
        OrchestratingEvolver._evolve_extract = original


def assert_no_source(kernel: Any, scope: Any, returned_units: list[Any]) -> None:
    from jiuwen_memory.common.type_def import MESSAGES_KEY_PREFIX

    messages = kernel.kv.scan(scope, prefix=MESSAGES_KEY_PREFIX)
    if messages:
        raise RuntimeError(
            f"no-source contract failed: found {len(messages)} raw /messages entries"
        )
    for unit in returned_units:
        if getattr(unit, "source_ref", None) is not None:
            raise RuntimeError(
                f"no-source contract failed: unit {unit.id} retained source_ref"
            )
        metadata = getattr(unit, "system_metadata", {}) or {}
        if "evidence" in metadata:
            raise RuntimeError(
                f"no-source contract failed: unit {unit.id} retained evidence"
            )
        if str(metadata.get("source_retained", "")).lower() != "false":
            raise RuntimeError(
                f"no-source contract failed: unit {unit.id} lacks source_retained=false"
            )
