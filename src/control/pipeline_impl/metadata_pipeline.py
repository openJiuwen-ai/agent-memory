"""基于 metadata / query.extensions 的 pipeline 路由实现。"""

from __future__ import annotations

from collections.abc import Mapping

from common.errors import ValidationError
from common.log import get_logger
from common.type_def import FilterOp, MemoryUnit
from construction.classifier import ClassifierProducer
from construction.consolidation import ConsolidatorProducer
from construction.evolver import EvolverProducer
from construction.index_builder import IndexBuilderProducer
from control.base import ControlOperatorType
from control.pipeline import MemoryPipeline, PipelineBinding, PipelineProducer
from retrieval.retriever import RetrieverProducer
from retrieval.types import RetrievalQuery

logger = get_logger(__name__)


class MetadataPipeline(MemoryPipeline):
    """按 ``memory_type`` 等字符串键选择 profile。

    写入侧从 ``MemoryUnit.metadata[route_key]`` 读取；查询侧优先从
    ``RetrievalQuery.extensions[route_key]`` 读取，其次从等值 filters 读取
    ``route_key`` 或 ``metadata.<route_key>``。
    """

    def __init__(
        self,
        profiles: dict[str, PipelineBinding],
        routes: dict[str, str],
        fallback: str,
        route_key: str = "memory_type",
    ) -> None:
        if fallback not in profiles:
            raise ValidationError(
                f"MetadataPipeline fallback profile {fallback!r} 不存在"
                f"（已定义：{sorted(profiles)}）"
            )
        self._profiles = profiles
        self._routes = dict(routes)
        self._fallback = fallback
        self._route_key = route_key

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PIPELINE

    def health(self) -> None:
        for binding in self._profiles.values():
            binding.index_builder.health()
            binding.retriever.health()
            binding.evolver.health()
            if binding.classifier is not None:
                binding.classifier.health()
            if binding.consolidator is not None:
                binding.consolidator.health()

    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        value = _route_value_from_units(units, self._route_key)
        return self._select(value, "write")

    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        value = _route_value_from_query(query, self._route_key)
        return self._select(value, "recall")

    def _select(self, value: str, surface: str) -> PipelineBinding:
        profile_name = self._routes.get(value, value) if value else self._fallback
        if profile_name not in self._profiles:
            logger.warning(
                "MetadataPipeline.%s: route value %r resolved to missing profile %r, fallback=%r",
                surface,
                value,
                profile_name,
                self._fallback,
            )
            profile_name = self._fallback
        binding = self._profiles[profile_name]
        logger.info(
            "MetadataPipeline.%s: route_key=%s value=%r profile=%s",
            surface,
            self._route_key,
            value,
            binding.name,
        )
        return binding


def _route_value_from_units(units: list[MemoryUnit], route_key: str) -> str:
    for unit in units:
        value = str(unit.metadata.get(route_key, "")).strip()
        if value:
            return value
    return ""


def _route_value_from_query(query: RetrievalQuery, route_key: str) -> str:
    value = str(query.extensions.get(route_key, "")).strip()
    if value:
        return value
    accepted_fields = {route_key, f"metadata.{route_key}"}
    for clause in query.filters:
        if clause.field in accepted_fields and clause.op == FilterOp.EQ:
            return str(clause.value).strip()
    return ""


def _string_param(raw: object, *, field: str, default: str | None = None) -> str:
    if raw is None:
        if default is None:
            raise ValidationError(f"pipeline profile 缺少必填字段 {field!r}")
        return default
    value = str(raw).strip()
    if not value:
        if default is None:
            raise ValidationError(f"pipeline profile 字段 {field!r} 不能为空")
        return default
    return value


def _build_profile(config, name: str, raw: object) -> PipelineBinding:
    if not isinstance(raw, Mapping):
        raise ValidationError(
            f"pipeline profile {name!r} 应是映射，得到 {type(raw).__name__}"
        )
    index_builder_name = _string_param(raw.get("index_builder"), field="index_builder")
    retriever_name = _string_param(raw.get("retriever"), field="retriever")
    evolver_name = _string_param(raw.get("evolver"), field="evolver")
    classifier_name = raw.get("classifier")
    classifier = None
    if classifier_name is not None and str(classifier_name).strip():
        classifier = ClassifierProducer.build_named(str(classifier_name).strip(), config.ctx)
    consolidator_name = raw.get("consolidator")
    consolidator = None
    if consolidator_name is not None and str(consolidator_name).strip():
        consolidator = ConsolidatorProducer.build_named(
            str(consolidator_name).strip(), config.ctx
        )
    return PipelineBinding(
        name=name,
        index_builder=IndexBuilderProducer.build_named(index_builder_name, config.ctx),
        retriever=RetrieverProducer.build_named(retriever_name, config.ctx),
        evolver=EvolverProducer.build_named(evolver_name, config.ctx),
        classifier=classifier,
        consolidator=consolidator,
    )


@PipelineProducer.register("metadata")
def _build(config):
    profiles_config = config.get("profiles")
    if not isinstance(profiles_config, Mapping) or not profiles_config:
        raise ValidationError("pipeline.metadata params.profiles 必须配置至少一个 profile")
    profiles = {
        str(name): _build_profile(config, str(name), raw)
        for name, raw in profiles_config.items()
    }
    routes_raw = config.get("routes", {})
    if not isinstance(routes_raw, Mapping):
        raise ValidationError("pipeline.metadata params.routes 必须是映射")
    routes = {str(key): str(value) for key, value in routes_raw.items()}
    return MetadataPipeline(
        profiles=profiles,
        routes=routes,
        fallback=config.get("fallback", "default"),
        route_key=config.get("route_key", "memory_type"),
    )
