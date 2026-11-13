# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""查询选择已装配的完整 Retriever；不识别插件实现或访问私有依赖。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    Scope,
    and_merge,
)
from jiuwen_memory.common.type_def.filter import MEMORY_TYPE_FILTER_FIELD
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.control.pipeline import PipelineBinding
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, TrajectoryStep

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrievalPipelineBinding:
    """开发者配置的完整 Retriever 及其固定类型范围；名称不承载类型语义。"""

    retriever: Retriever
    memory_types: tuple[str, ...] = ()
    exclude_memory_types: tuple[str, ...] = ()

    def type_filter(self) -> FilterExpr | None:
        clauses: list[FilterExpr] = []
        if self.memory_types:
            clauses.append(FilterClause(
                MEMORY_TYPE_FILTER_FIELD,
                FilterOp.EQ if len(self.memory_types) == 1 else FilterOp.IN,
                self.memory_types[0] if len(self.memory_types) == 1 else list(self.memory_types),
            ))
        if self.exclude_memory_types:
            clauses.append(FilterClause(
                MEMORY_TYPE_FILTER_FIELD, FilterOp.NOT_IN, list(self.exclude_memory_types),
            ))
        return and_merge(None, clauses)


def _type_names(raw: object, key: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"{key} must be a non-empty list of memory types")
    if any(
        not isinstance(value, str) or not value.strip() or value != value.strip() for value in raw
    ):
        raise ValidationError(f"{key} must contain non-empty, unpadded strings")
    return tuple(dict.fromkeys(raw))


def build_retrieval_pipelines(
    config: ComponentConfig,
) -> dict[str, Retriever | RetrievalPipelineBinding] | None:
    """装配全部具名引用；None 保留旧装配，显式空映射不公开任何名称。"""
    if "retrieval_pipelines" not in config.params:
        return None
    references = config.params["retrieval_pipelines"]
    if not isinstance(references, dict):
        raise ValidationError("retrieval_pipelines must be a mapping of public names to retrievers")
    for public, reference in references.items():
        if not isinstance(public, str) or not public.strip() or public != public.strip():
            raise ValidationError("retrieval_pipelines keys must be non-empty strings")
        if isinstance(reference, dict):
            unknown = set(reference) - {"retriever", "memory_types", "exclude_memory_types"}
            if unknown:
                raise ValidationError(
                    f"retrieval_pipelines.{public}: unknown keys {sorted(unknown)}"
                )
            reference = reference.get("retriever")
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or reference != reference.strip()
        ):
            raise ValidationError(f"retrieval_pipelines.{public} must reference a named retriever")
    result: dict[str, Retriever | RetrievalPipelineBinding] = {}
    for public, declaration in references.items():
        if isinstance(declaration, str):
            result[public] = RetrieverProducer.build_named(declaration, config.ctx)
            continue
        included = (
            _type_names(declaration["memory_types"], f"retrieval_pipelines.{public}.memory_types")
            if "memory_types" in declaration else ()
        )
        excluded = (
            _type_names(
                declaration["exclude_memory_types"],
                f"retrieval_pipelines.{public}.exclude_memory_types",
            ) if "exclude_memory_types" in declaration else ()
        )
        if set(included) & set(excluded):
            raise ValidationError(
                f"retrieval_pipelines.{public}: included and excluded types overlap"
            )
        result[public] = RetrievalPipelineBinding(
            RetrieverProducer.build_named(declaration["retriever"], config.ctx), included, excluded,
        )
    return result


class RetrievalPipelines:
    """各引擎复用的选择器；请求状态只存在于栈上，不修改共享实例。"""

    def __init__(
        self, default: Retriever,
        pipelines: dict[str, Retriever | RetrievalPipelineBinding] | None,
    ) -> None:
        self._default = default
        bindings = pipelines if pipelines is not None else {"default": default}
        self._pipelines = {
            name: value.retriever if isinstance(value, RetrievalPipelineBinding) else value
            for name, value in bindings.items()
        }
        self._type_filters = {
            name: value.type_filter()
            for name, value in bindings.items() if isinstance(value, RetrievalPipelineBinding)
        }
        if pipelines:
            parsers = default.query_parsers()
            if not parsers or parsers[0] is None or any(p is not parsers[0] for p in parsers):
                raise ValidationError(
                    "default retriever must declare one shared QueryParser via query_parsers()"
                )
            for name, retriever in self._pipelines.items():
                declared = retriever.query_parsers()
                if not declared or any(parser is not parsers[0] for parser in declared):
                    raise ValidationError(
                        f"retrieval_pipelines.{name}: query_parsers() must declare the default "
                        "retriever's shared parser; configure query_parser with the same named "
                        "reference (anonymous or different parsers are not supported)"
                    )

    def names(self) -> tuple[str, ...]:
        return tuple(self._pipelines)

    def type_filter(self, name: str | None) -> FilterExpr | None:
        return deepcopy(self._type_filters.get(name))

    def prepare(self, query: RetrievalQuery) -> RetrievalQuery:
        constraint = self.type_filter(query.pipeline)
        if constraint is None or _requires(query.filters, constraint):
            return query
        return replace(query, filters=and_merge(query.filters, [constraint]))

    def health(self) -> None:
        seen: set[int] = set()
        for retriever in (self._default, *self._pipelines.values()):
            if id(retriever) not in seen:
                seen.add(id(retriever))
                retriever.health()

    def retrieve(
        self, scope: Scope, query: RetrievalQuery, binding: PipelineBinding | None,
    ) -> RetrievalResult:
        # 名称由 MemoryEngine 的公共校验方法在调用本方法前确认。
        if query.pipeline is not None:
            retriever, name, source = self._pipelines[query.pipeline], query.pipeline, "query"
        elif binding is not None:
            retriever, name, source = binding.retriever, binding.name, "profile"
        else:
            retriever, name, source = self._default, "default", "default"
        logger.debug("Retriever selected: pipeline=%s source=%s", name, source)
        result = retriever.retrieve(scope, query)
        if query.with_trajectory:
            # 保留 Prepared/SchemaTemporal 结果子类型及内部展开来源，只更新轨迹。
            result.trajectory.insert(0, TrajectoryStep(
                stage="pipeline", detail={"name": name, "source": source},
            ))
        return result


def _requires(expression: FilterExpr | None, constraint: FilterExpr) -> bool:
    """仅对已有 AND 子条件判重；OR/NOT 内出现不能证明边界已强制。"""
    if expression == constraint:
        return True
    if isinstance(expression, FilterGroup) and expression.logic is FilterLogic.AND:
        return any(_requires(child, constraint) for child in expression.children)
    return False
