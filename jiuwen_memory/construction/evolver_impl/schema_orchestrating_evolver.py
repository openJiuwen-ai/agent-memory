"""Source-first orchestration for the opt-in Entity Schema extractor.

The official :class:`OrchestratingEvolver` remains unchanged. This extension only
overrides the non-procedural EXTRACT route so raw evidence is durable before the
fallible LLM extraction and every accepted Schema property is added as one normal
``MemoryUnit`` without ordinary similarity deduplication.
"""

from __future__ import annotations

import copy

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer
from jiuwen_memory.construction.prompt_strategy import copy_consolidation_prompts
from jiuwen_memory.storage.storage import StorageProducer

logger = get_logger(__name__)


class SchemaOrchestratingEvolver(OrchestratingEvolver):
    """Persist source evidence first, then add accepted Schema property units."""

    def _persist_source_evidence(self, units: list[MemoryUnit]) -> list[str]:
        created: list[str] = []
        for unit in units:
            if self._storage.get(unit.scope, [unit.id]):
                continue
            source = copy.deepcopy(unit)
            source.system_metadata = dict(source.system_metadata)
            source.system_metadata.update(
                {
                    "memory_role": "source_evidence",
                    "schema_source_evidence": True,
                }
            )
            source.tags = merge_unit_tags(source.tags, ["source_evidence"])
            created.extend(self._persist([source]))
        return created

    def _evolve_extract(self, units: list[MemoryUnit]) -> EvolveResult:
        if self._is_procedural(units):
            return super()._evolve_extract(units)
        if not units:
            return EvolveResult()

        # Source persistence is the durability boundary and intentionally fails loudly.
        source_ids = self._persist_source_evidence(units)
        recent = self._persist_and_maintain_messages(units)
        context = self._maybe_collect_extract_context(units, recent)
        try:
            extracted = self._extractor.extract(units, context=context)
        except Exception as exc:
            logger.warning(
                "SchemaOrchestratingEvolver.schema_extract_degraded: source_units=%d error=%s: %s",
                len(units),
                type(exc).__name__,
                exc,
            )
            self.last_schema_error = f"{type(exc).__name__}: {exc}"
            return EvolveResult(created_ids=source_ids)

        self.last_schema_error = ""
        if not extracted:
            return EvolveResult(created_ids=source_ids)

        copy_consolidation_prompts(units, extracted)
        self._annotate_layers(extracted)
        property_ids = self._persist(extracted)
        logger.info(
            "SchemaOrchestratingEvolver: source=%d properties=%d",
            len(source_ids),
            len(property_ids),
        )
        return EvolveResult(created_ids=[*source_ids, *property_ids])


def _optional_layer_annotator(config):
    namespace = config.ctx.namespaces.get(LayerAnnotatorProducer.TOP_NAME, {})
    if "default" not in namespace:
        return None
    return LayerAnnotatorProducer.build_named("default", config.ctx)


@EvolverProducer.register("schema_orchestrating")
def _build(config):
    vector_on = config.get("vector_enabled", True)
    index_default = "hybrid" if vector_on else "fulltext"
    dedup_default = "vector" if vector_on else "keyword"
    return SchemaOrchestratingEvolver(
        extractor=ExtractorProducer.dep(config, default="entity_schema"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=IndexBuilderProducer.dep(config, "index_builder", default=index_default),
        storage=StorageProducer.resolve(config),
        dedup=DedupProducer.dep(config, default=dedup_default),
        llm=LlmProducer.dep(config, default="echo"),
        layer_annotator=_optional_layer_annotator(config),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
    )
