# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""查询选择已装配的完整 Retriever；不识别插件实现或访问私有依赖。"""

from __future__ import annotations

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.control.pipeline import PipelineBinding
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, TrajectoryStep

logger = get_logger(__name__)


def build_retrieval_pipelines(config: ComponentConfig) -> dict[str, Retriever] | None:
    """装配全部具名引用；None 保留旧装配，显式空映射不公开任何名称。"""
    if "retrieval_pipelines" not in config.params:
        return None
    references = config.params["retrieval_pipelines"]
    if not isinstance(references, dict):
        raise ValidationError("retrieval_pipelines must be a mapping of public names to retrievers")
    for public, reference in references.items():
        if not isinstance(public, str) or not public.strip():
            raise ValidationError("retrieval_pipelines keys must be non-empty strings")
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError(f"retrieval_pipelines.{public} must reference a named retriever")
    return {
        public: RetrieverProducer.build_named(reference, config.ctx)
        for public, reference in references.items()
    }


class RetrievalPipelines:
    """各引擎复用的选择器；请求状态只存在于栈上，不修改共享实例。"""

    def __init__(self, default: Retriever, pipelines: dict[str, Retriever] | None) -> None:
        self._default = default
        self._pipelines = dict(pipelines) if pipelines is not None else {"default": default}
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
