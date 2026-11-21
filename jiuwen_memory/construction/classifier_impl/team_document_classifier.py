# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Assign the protected team document class through an explicit write profile."""
from __future__ import annotations

from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.classifier import Classifier, ClassifierProducer


class TeamDocumentClassifier(Classifier):
    """Preserve the configured classification behavior without re-extracting content."""

    def __init__(self, delegate: Classifier | None = None) -> None:
        self._delegate = delegate

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        if self._delegate is not None:
            self._delegate.health()

    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        for unit in units:
            unit.system_metadata["memory_class"] = "team_memory"
        if self._delegate is not None:
            return self._delegate.classify(units)
        return units


@ClassifierProducer.register("team_document")
def _build(config):
    delegate = None
    if config.params.get("base_classifier"):
        delegate = ClassifierProducer.dep(config, "base_classifier")
    return TeamDocumentClassifier(delegate)
