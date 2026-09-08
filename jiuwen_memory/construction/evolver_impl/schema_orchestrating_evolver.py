"""Source-first orchestration for the opt-in Entity Schema extractor.

The official :class:`OrchestratingEvolver` remains unchanged. This extension only
overrides the non-procedural EXTRACT route so raw evidence is durable before the
fallible LLM extraction and every accepted Schema property is added as one normal
``MemoryUnit`` without ordinary similarity deduplication.
"""

from __future__ import annotations

import copy

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.abstractor import Abstractor, AbstractorProducer
from jiuwen_memory.construction.associator import Associator, AssociatorProducer
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.dedup import Dedup, DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    OrchestratingEvolver,
    _resolve_message_store,
)
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.construction.layer_annotator import LayerAnnotator, LayerAnnotatorProducer
from jiuwen_memory.construction.prompt_strategy import copy_consolidation_prompts
from jiuwen_memory.storage.kv import KVStore, load_units
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import IndexWriteMode

logger = get_logger(__name__)


class SchemaOrchestratingEvolver(OrchestratingEvolver):
    """Persist source evidence first, then add accepted Schema property units."""

    def __init__(
        self,
        extractor: Extractor,
        abstractor: Abstractor,
        associator: Associator,
        index_builder: IndexBuilder,
        storage: StoreManager,
        message_store: KVStore,
        dedup: Dedup,
        llm: LLM,
        layer_annotator: LayerAnnotator | None = None,
        *,
        kv_name: str = "default",
        dedup_medium_similarity: float = 0.7,
        dedup_high_similarity: float = 0.9,
    ) -> None:
        super().__init__(
            extractor=extractor,
            abstractor=abstractor,
            associator=associator,
            index_builder=index_builder,
            storage=storage,
            message_store=message_store,
            dedup=dedup,
            llm=llm,
            layer_annotator=layer_annotator,
            dedup_medium_similarity=dedup_medium_similarity,
            dedup_high_similarity=dedup_high_similarity,
        )
        # Official IndexBuilder owns all writes. KV port is retained for source reads only.
        self._source_kv = storage.kv(kv_name)

    def _persist_source_evidence(self, units: list[MemoryUnit]) -> list[str]:
        created: list[str] = []
        for unit in units:
            if load_units(self._source_kv, unit.scope, [unit.id]):
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

    def _write_schema_entities_to_sources(
        self,
        source_units: list[MemoryUnit],
        property_units: list[MemoryUnit],
    ) -> list[str]:
        """Write extracted entity/property terms back to their persisted sources."""
        source_by_id = {unit.id: unit for unit in source_units}
        terms_by_source: dict[str, list[str]] = {unit.id: [] for unit in source_units}
        for property_unit in property_units:
            entity_name = str(
                property_unit.system_metadata.get("schema_entity_name") or ""
            ).strip()
            property_name = str(
                property_unit.system_metadata.get("schema_property_name") or ""
            ).strip()
            terms = [term for term in (entity_name, property_name) if term]
            if not terms:
                continue
            referenced_ids = property_unit.provenance
            if not referenced_ids and property_unit.source_ref:
                referenced_ids = [property_unit.source_ref]
            for source_id in referenced_ids:
                if source_id in source_by_id:
                    terms_by_source[source_id].extend(terms)

        updated_ids: list[str] = []
        for source_id, terms in terms_by_source.items():
            if not terms:
                continue
            input_source = source_by_id[source_id]
            stored = load_units(self._source_kv, input_source.scope, [source_id])
            if not stored:
                logger.warning(
                    "SchemaOrchestratingEvolver: persisted source %s is missing during writeback",
                    source_id,
                )
                continue
            source = copy.deepcopy(stored[0])
            source.entities = list(dict.fromkeys([*source.entities, *terms]))
            self._index.update([source], mode=IndexWriteMode.ALL)
            updated_ids.append(source.id)
        return updated_ids

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
        source_writeback_ids = self._write_schema_entities_to_sources(units, extracted)
        existing_source_updates = [
            source_id for source_id in source_writeback_ids if source_id not in source_ids
        ]
        logger.info(
            "SchemaOrchestratingEvolver: source=%d properties=%d source_writebacks=%d",
            len(source_ids),
            len(property_ids),
            len(source_writeback_ids),
        )
        return EvolveResult(
            created_ids=[*source_ids, *property_ids],
            updated_ids=existing_source_updates,
        )


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
    storage = StoreManagerProducer.resolve(config)
    return SchemaOrchestratingEvolver(
        extractor=ExtractorProducer.dep(config, default="entity_schema"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=IndexBuilderProducer.dep(config, "index_builder", default=index_default),
        storage=storage,
        message_store=_resolve_message_store(config),
        dedup=DedupProducer.dep(config, default=dedup_default),
        llm=LlmProducer.dep(config, default="echo"),
        layer_annotator=_optional_layer_annotator(config),
        kv_name=resolve_name(config, "kv_store"),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
    )
