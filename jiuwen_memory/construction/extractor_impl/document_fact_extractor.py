# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Document facts require an explicit distinction between profile and project facts."""
from __future__ import annotations

from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import (
    AgentLlmExtractor,
    build_agent_extractor,
)
from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractionCandidate


class DocumentFactExtractor(AgentLlmExtractor):
    """Validate the output contract independently of the originating application."""

    def build_candidates(
        self, items: list[dict], units: list[MemoryUnit],
    ) -> list[ExtractionCandidate]:
        accepted = [
            item for item in items
            if isinstance(item, dict) and isinstance(item.get("project_fact"), bool)
        ]
        return super().build_candidates(accepted, units)


@ExtractorProducer.register("document_fact")
def _build(config):
    return build_agent_extractor(config, DocumentFactExtractor)
