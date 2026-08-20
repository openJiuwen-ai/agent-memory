"""Schema-aware orchestration without modifying the official evolvers.

The control flow is copied from the Schema branches in agent-memory's orchestrating
evolver and adapted only for mem2 package paths, ``system_metadata`` and its four-field
``EvolveResult``. Raw evidence is persisted before fallible non-procedural extraction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.abstractor import AbstractorProducer
from jiuwen_memory.construction.associator import AssociatorProducer
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.evolver import EvolveResult, EvolverProducer
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import SchemaEntityRegistry
from jiuwen_memory.construction.evolver_impl.schema_entity_resolver import SchemaEntityResolver
from jiuwen_memory.construction.evolver_impl.schema_property_merge import (
    SchemaPropertyMergeExecutor,
    SchemaPropertyMergePlanner,
)
from jiuwen_memory.construction.extractor import ExtractorProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer
from jiuwen_memory.construction.prompt_strategy import copy_consolidation_prompts
from jiuwen_memory.storage.storage import Storage, StorageProducer

logger = get_logger(__name__)

_PROPERTY_MODE = "schema"
_ENTITY_OBSERVATION_MODE = "schema_entity_observation"
_RELATION_MODE = "schema_relation"


@dataclass(frozen=True)
class _SchemaComponents:
    resolver: SchemaEntityResolver | None
    registry: SchemaEntityRegistry | None
    planner: SchemaPropertyMergePlanner
    executor: SchemaPropertyMergeExecutor


class SchemaEvolverMixin:
    """Shared Schema routing copied by isolated orchestrating and dynamic evolvers."""

    def _configure_schema_extension(
        self,
        *,
        schema_entity_resolver: SchemaEntityResolver | None,
        schema_entity_registry: SchemaEntityRegistry | None,
        schema_property_merge_planner: SchemaPropertyMergePlanner | None,
        schema_property_merge_executor: SchemaPropertyMergeExecutor | None,
    ) -> None:
        self._schema_entity_resolver = schema_entity_resolver
        self._schema_entity_registry = schema_entity_registry
        self._schema_property_merge_planner = schema_property_merge_planner
        self._schema_property_merge_executor = schema_property_merge_executor

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

    def _resolve_schema_identities(
        self,
        candidates: list[MemoryUnit],
        entity_observations: list[MemoryUnit],
    ) -> bool:
        resolver = getattr(self, "_schema_entity_resolver", None)
        if resolver is None:
            return True
        try:
            resolver.resolve([*candidates, *entity_observations])
        except Exception as exc:
            logger.error(
                "%s: entity resolution failed; discard unresolved Schema derivations: %s",
                type(self).__name__,
                exc,
                exc_info=True,
            )
            self.last_schema_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    def _sync_schema_entity_registry(self, observations: list[MemoryUnit]) -> None:
        registry = getattr(self, "_schema_entity_registry", None)
        if registry is not None:
            registry.sync(observations)

    def _evolve_schema_candidates(self, candidates: list[MemoryUnit]) -> EvolveResult:
        planner = getattr(self, "_schema_property_merge_planner", None)
        executor = getattr(self, "_schema_property_merge_executor", None)
        if planner is None or executor is None:
            return EvolveResult(created_ids=self._persist(candidates))

        execution = executor.apply(planner.plan(candidates))
        logger.info(
            "%s.property_merge: candidates=%d add=%d supersede=%d archive=%d",
            type(self).__name__,
            len(candidates),
            len(execution.created_ids),
            len(execution.superseded_ids),
            len(execution.archived_ids),
        )
        # mem2 EvolveResult has no archived_ids. Archive is an update of an existing unit.
        return EvolveResult(
            created_ids=execution.created_ids,
            updated_ids=[*execution.updated_ids, *execution.archived_ids],
            superseded_ids=execution.superseded_ids,
        )

    def _evolve_schema_procedural(self, units: list[MemoryUnit]) -> EvolveResult:
        """Copy the original procedural Schema route: extract, resolve, merge, persist."""

        extracted = self._extractor.extract(units, context=None)
        logger.info(
            "%s: EXTRACT(procedural) extractor returned %d units",
            type(self).__name__,
            len(extracted),
        )
        if not extracted:
            return EvolveResult()
        copy_consolidation_prompts(units, extracted)
        self._annotate_layers(extracted)
        return self._route_orchestrating_candidates(extracted)

    def _route_orchestrating_candidates(self, extracted: list[MemoryUnit]) -> EvolveResult:
        """Route ordinary and Schema candidates exactly once, preserving internal intents."""

        relations, entity_observations, schema_candidates, ordinary_candidates = (
            _partition_extracted(extracted)
        )
        candidates = [*ordinary_candidates, *schema_candidates]
        identities_resolved = self._resolve_schema_identities(candidates, entity_observations)

        result = self._dedup_batch(ordinary_candidates) if ordinary_candidates else EvolveResult()
        if schema_candidates and identities_resolved:
            schema_result = self._evolve_schema_candidates(schema_candidates)
            _merge_evolve_results(result, schema_result)

        if identities_resolved:
            self._sync_schema_entity_registry([*candidates, *entity_observations])
        if relations:
            logger.info(
                "%s: ignored %d relation intents; graph projection is not enabled",
                type(self).__name__,
                len(relations),
            )
        return result


class SchemaOrchestratingEvolver(SchemaEvolverMixin, OrchestratingEvolver):
    """Source-first Schema extraction, Entity Identity and Property Merge."""

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

        # Source persistence is the durability boundary and must fail loudly.
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
        result = self._route_orchestrating_candidates(extracted)
        result.created_ids = [*source_ids, *result.created_ids]
        return result


def _partition_extracted(
    extracted: list[MemoryUnit],
) -> tuple[list[MemoryUnit], list[MemoryUnit], list[MemoryUnit], list[MemoryUnit]]:
    relations: list[MemoryUnit] = []
    entity_observations: list[MemoryUnit] = []
    schema_candidates: list[MemoryUnit] = []
    ordinary_candidates: list[MemoryUnit] = []
    for unit in extracted:
        mode = unit.system_metadata.get("extraction_mode")
        if mode == _RELATION_MODE:
            relations.append(unit)
        elif mode == _ENTITY_OBSERVATION_MODE:
            entity_observations.append(unit)
        elif mode == _PROPERTY_MODE:
            schema_candidates.append(unit)
        else:
            ordinary_candidates.append(unit)
    return relations, entity_observations, schema_candidates, ordinary_candidates


def _merge_evolve_results(target: EvolveResult, source: EvolveResult) -> None:
    target.created_ids.extend(source.created_ids)
    target.updated_ids.extend(source.updated_ids)
    target.superseded_ids.extend(source.superseded_ids)
    target.forgotten_ids.extend(source.forgotten_ids)


def _first_config_value(config, *keys: str):
    for key in keys:
        value = config.get(key)
        if value is not None:
            return value
    return None


def _build_schema_components(
    config,
    *,
    storage: Storage,
    index_builder: IndexBuilder,
    llm: LLM,
    embedder: Embedder,
) -> _SchemaComponents:
    entity_resolution_enabled = as_bool(
        config.get("schema_entity_resolution_enabled"),
        default=True,
    )
    merge_decision_enabled = as_bool(
        _first_config_value(
            config,
            "schema_entity_merge_decision_enabled",
            "schema_entity_merge_enabled",
        ),
        default=True,
    )
    resolver = (
        SchemaEntityResolver(
            storage=storage,
            embedder=embedder,
            llm=llm,
            enable_merge_decision=merge_decision_enabled,
            recall_top_k=int(config.get("schema_entity_recall_top_k", 15)),
            max_merge_retries=int(config.get("schema_entity_max_merge_retries", 8)),
            kv_fallback_limit=int(config.get("schema_entity_kv_fallback_limit", 1000)),
        )
        if entity_resolution_enabled
        else None
    )
    registry = (
        SchemaEntityRegistry(storage=storage, embedder=embedder)
        if entity_resolution_enabled
        else None
    )
    # ``use_property_merge`` is the original/MindMemOS-compatible key and wins when both exist.
    property_merge_enabled = as_bool(
        _first_config_value(config, "use_property_merge", "schema_property_merge_enabled"),
        default=False,
    )
    planner = SchemaPropertyMergePlanner(
        storage=storage,
        embedder=embedder,
        llm=llm,
        top_k=int(config.get("schema_property_merge_top_k", 5)),
        vector_candidate_multiplier=int(
            config.get("schema_property_merge_vector_candidate_multiplier", 4)
        ),
        kv_fallback_limit=int(config.get("schema_property_merge_kv_fallback_limit", 1000)),
        merge_enabled=property_merge_enabled,
    )
    return _SchemaComponents(
        resolver=resolver,
        registry=registry,
        planner=planner,
        executor=SchemaPropertyMergeExecutor(storage=storage, index_builder=index_builder),
    )


def _optional_layer_annotator(config):
    namespace = config.ctx.namespaces.get(LayerAnnotatorProducer.TOP_NAME, {})
    if "default" not in namespace:
        return None
    return LayerAnnotatorProducer.build_named("default", config.ctx)


def _resolve_embedder(config) -> Embedder:
    namespace = config.ctx.namespaces.get(EmbedderProducer.TOP_NAME, {})
    if "default" in namespace:
        return EmbedderProducer.build_named("default", config.ctx)
    return EmbedderProducer.dep(config, default="hashing")


@EvolverProducer.register("schema_orchestrating")
def _build(config):
    vector_on = config.get("vector_enabled", True)
    index_default = "hybrid" if vector_on else "fulltext"
    dedup_default = "vector" if vector_on else "keyword"
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

    return SchemaOrchestratingEvolver(
        extractor=ExtractorProducer.dep(config, default="entity_schema"),
        abstractor=AbstractorProducer.dep(config, default="concat"),
        associator=AssociatorProducer.dep(config, default="keyword"),
        index_builder=index_builder,
        storage=storage,
        dedup=DedupProducer.dep(config, default=dedup_default),
        llm=llm,
        layer_annotator=_optional_layer_annotator(config),
        dedup_medium_similarity=config.get("dedup_medium_similarity", 0.7),
        dedup_high_similarity=config.get("dedup_high_similarity", 0.9),
        schema_entity_resolver=components.resolver,
        schema_entity_registry=components.registry,
        schema_property_merge_planner=components.planner,
        schema_property_merge_executor=components.executor,
    )
