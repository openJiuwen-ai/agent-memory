"""基于 metadata / query.extensions 的 pipeline 路由实现。"""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    MEMORY_TYPE_FILTER_FIELD,
    MemoryUnit,
    extract_required_equality,
)
from jiuwen_memory.construction.classifier import ClassifierProducer
from jiuwen_memory.construction.evolver import EvolverProducer
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.pipeline import MemoryPipeline, PipelineBinding, PipelineProducer
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery

logger = get_logger(__name__)


class MetadataPipeline(MemoryPipeline):
    """按 ``memory_type`` 等字符串键选择 profile。

    写入侧从 ``MemoryUnit.system_metadata[route_key]`` 读取；查询侧优先从
    ``RetrievalQuery.extensions[route_key]`` 读取，其次从规范字段
    ``system_metadata.<route_key>`` 的等值 filters 读取。
    """

    def __init__(
        self,
        profiles: dict[str, PipelineBinding],
        routes: dict[str, str],
        fallback: str,
        route_key: str = "memory_type",
    ) -> None:
        """初始化 MetadataPipeline。

        Args:
            profiles: 参数 profiles（dict[str, PipelineBinding]）。
            routes: 参数 routes（dict[str, str]）。
            fallback: 参数 fallback（str）。
            route_key: 参数 route_key（str）。

        Raises:
            ValidationError: 执行失败时抛出。
        """
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
        """返回当前算子类型。

        Returns:
            返回 ControlOperatorType。
        """
        return ControlOperatorType.PIPELINE

    def health(self) -> None:
        """执行健康检查。"""
        for binding in self._profiles.values():
            binding.index_builder.health()
            binding.retriever.health()
            binding.evolver.health()
            if binding.classifier is not None:
                binding.classifier.health()

    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        """执行 `select_for_write` 操作。

        Args:
            units: 参数 units（list[MemoryUnit]）。

        Returns:
            返回 PipelineBinding。
        """
        value = _route_value_from_units(units, self._route_key)
        return self._select(value, "write")

    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        """执行 `select_for_recall` 操作。

        Args:
            query: 参数 query（RetrievalQuery）。

        Returns:
            返回 PipelineBinding。
        """
        value = _route_value_from_query(query, self._route_key)
        return self._select(value, "recall")

    def _select(self, value: str, surface: str) -> PipelineBinding:
        """执行 `select` 操作。

        Args:
            value: 参数 value（str）。
            surface: 参数 surface（str）。

        Returns:
            返回 PipelineBinding。
        """
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
    """执行 `route_value_from_units` 操作。

    Args:
        units: 参数 units（list[MemoryUnit]）。
        route_key: 参数 route_key（str）。

    Returns:
        返回 str。
    """
    for unit in units:
        value = str(unit.system_metadata.get(route_key, "")).strip()
        if value:
            return value
    return ""


def _route_value_from_query(query: RetrievalQuery, route_key: str) -> str:
    """执行 `route_value_from_query` 操作。

    Args:
        query: 参数 query（RetrievalQuery）。
        route_key: 参数 route_key（str）。

    Returns:
        返回 str。
    """
    value = str(query.extensions.get(route_key, "")).strip()
    if value:
        return value
    # query.filters 已在 RetrievalQuery 边界规范化；memory_type 使用共享规范字段常量。
    filter_field = (
        MEMORY_TYPE_FILTER_FIELD
        if route_key == "memory_type"
        else f"system_metadata.{route_key}"
    )
    routed = extract_required_equality(query.filters, filter_field)
    return str(routed).strip() if routed is not None else ""


def _string_param(raw: object, *, field: str, default: str | None = None) -> str:
    """执行 `string_param` 操作。

    Args:
        raw: 参数 raw（object）。
        field: 参数 field（str）。
        default: 参数 default（str | None）。

    Returns:
        返回 str。

    Raises:
        ValidationError: 执行失败时抛出。
    """
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
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
        name: 参数 name（str）。
        raw: 参数 raw（object）。

    Returns:
        返回 PipelineBinding。

    Raises:
        ValidationError: 执行失败时抛出。
    """
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
    return PipelineBinding(
        name=name,
        index_builder=IndexBuilderProducer.build_named(index_builder_name, config.ctx),
        retriever=RetrieverProducer.build_named(retriever_name, config.ctx),
        evolver=EvolverProducer.build_named(evolver_name, config.ctx),
        classifier=classifier,
    )


@PipelineProducer.register("metadata")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。

    Raises:
        ValidationError: 执行失败时抛出。
    """
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
