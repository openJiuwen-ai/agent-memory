"""Isolated DynamicEvolver variant with the copied Schema routing branches."""

from __future__ import annotations

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.config.config_source import ConfigSourceProducer
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import SchemaEntityRegistry
from jiuwen_memory.construction.evolver_impl.schema_entity_resolver import SchemaEntityResolver
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaEvolverMixin,
    _build_schema_components,
    _merge_evolve_results,
    _optional_layer_annotator,
    _partition_extracted,
    _resolve_embedder,
)
from jiuwen_memory.construction.evolver_impl.schema_property_merge import (
    SchemaPropertyMergeExecutor,
    SchemaPropertyMergePlanner,
)
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.construction.prompt_registry import PromptRegistry
from jiuwen_memory.storage.storage import StorageProducer

logger = get_logger(__name__)


class SchemaDynamicEvolver(SchemaEvolverMixin, DynamicEvolver):
    """Dynamic four-step flow for ordinary units plus Schema Identity/Property Merge."""

    def __init__(
        self,
        *args,
        schema_entity_resolver: SchemaEntityResolver | None = None,
        schema_entity_registry: SchemaEntityRegistry | None = None,
        schema_property_merge_planner: SchemaPropertyMergePlanner | None = None,
        schema_property_merge_executor: SchemaPropertyMergeExecutor | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._configure_schema_extension(
            schema_entity_resolver=schema_entity_resolver,
            schema_entity_registry=schema_entity_registry,
            schema_property_merge_planner=schema_property_merge_planner,
            schema_property_merge_executor=schema_property_merge_executor,
        )

    def _evolve_extract(self, units: list[MemoryUnit]) -> EvolveResult:
        if self._is_procedural(units):
            return self._evolve_schema_procedural(units)
        if not units:
            return EvolveResult()

        source_ids = self._persist_source_evidence(units)
        recent = self._persist_and_maintain_messages(units)
        context = self._maybe_collect_extract_context(units, recent)
        try:
            extracted = self._extract_step(units, context)
        except Exception as exc:
            logger.warning(
                "SchemaDynamicEvolver.schema_extract_degraded: source_units=%d error=%s: %s",
                len(units),
                type(exc).__name__,
                exc,
            )
            self.last_schema_error = f"{type(exc).__name__}: {exc}"
            return EvolveResult(created_ids=source_ids)

        self.last_schema_error = ""
        if not extracted:
            return EvolveResult(created_ids=source_ids)

        relations, entity_observations, schema_candidates, ordinary_candidates = (
            _partition_extracted(extracted)
        )
        candidates = [*ordinary_candidates, *schema_candidates]
        self._resolve_schema_identities(candidates, entity_observations)

        if ordinary_candidates:
            decisions = self._consolidate_step(ordinary_candidates)
            reflected = self._reflect_step(ordinary_candidates, decisions)
            result = self._persist_decisions(reflected, decisions)
        else:
            result = EvolveResult()
        if schema_candidates:
            _merge_evolve_results(result, self._evolve_schema_candidates(schema_candidates))

        self._sync_schema_entity_registry([*candidates, *entity_observations])
        if relations:
            logger.info(
                "SchemaDynamicEvolver: ignored %d relation intents; "
                "graph projection is not enabled",
                len(relations),
            )
        result.created_ids = [*source_ids, *result.created_ids]
        return result


@EvolverProducer.register("schema_dynamic")
def _build(config):
    vector_on = config.get("vector_enabled", True)
    index_default = "hybrid" if vector_on else "fulltext"
    dedup_default = "vector" if vector_on else "keyword"

    prompts_data = config.get("prompts")
    config_source = ConfigSourceProducer.get_cached("default")
    registry = (
        PromptRegistry.from_dict(prompts_data, config_source=config_source)
        if prompts_data
        else PromptRegistry(config_source=config_source)
    )

    storage = StorageProducer.resolve(config)
    index_builder = IndexBuilderProducer.dep(config, "index_builder", default=index_default)
    llm = LlmProducer.dep(config, default="echo")
    embedder = _resolve_embedder(config)
    components = _build_schema_components(
        config,
        storage=storage,
        index_builder=index_builder,
        llm=llm,
        embedder=embedder,
    )
    return SchemaDynamicEvolver(
        extractor=ExtractorProducer.dep(config, default="entity_schema"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=index_builder,
        storage=storage,
        dedup=DedupProducer.dep(config, default=dedup_default),
        llm=llm,
        layer_annotator=_optional_layer_annotator(config),
        prompt_registry=registry,
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
        schema_entity_resolver=components.resolver,
        schema_entity_registry=components.registry,
        schema_property_merge_planner=components.planner,
        schema_property_merge_executor=components.executor,
    )
