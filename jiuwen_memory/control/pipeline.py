# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MemoryPipeline — 控制层的跨构建/查询 pipeline 编排抽象。

Pipeline 不实现抽取、索引或检索算法；它只根据写入单元或查询选项选择
一组已装配好的跨层算子绑定。这样 route 发生在 control 编排层，
construction / retrieval 仍保持不知道 control 的单向依赖边界。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
    ISOLATED_MEMORY_TYPES,
    MEMORY_TYPE_FILTER_FIELD,
    FilterExpr,
    MemoryUnit,
    MetadataValueType,
    extract_required_equality,
)
from jiuwen_memory.retrieval.types import RetrievalQuery

from .base import ControlOperator

if TYPE_CHECKING:
    from jiuwen_memory.construction.classifier import Classifier
    from jiuwen_memory.construction.evolver import Evolver
    from jiuwen_memory.construction.index_builder import IndexBuilder
    from jiuwen_memory.retrieval.retriever import Retriever


class PipelineProducer(Factory):
    """MemoryPipeline 的注册式工厂。

    配置顶层名为 ``pipeline``；具体实现通过 ``@PipelineProducer.register("<name>")``
    自注册，并在 control bootstrap 中统一 import。
    """

    TOP_NAME = "pipeline"


# 不回落到默认 profile 的 memory_type 取值（mem2.0 §2.1「experience 不回落」）：
# 带这些取值的请求只由对应 profile 处理，没有 profile 即参数错误，不静默回落——
# 回落时轨迹 JSON 会被当作一段对话抽取、写入用户自己的空间，而 add 仍返回成功。
NO_FALLBACK_MEMORY_TYPES = frozenset({"experience"})

# 按类型隔离的 memory_type 取值（mem2.0 §2.3「同一空间内的隔离」）：经验与技能
# 可与对话记忆落在同一个空间，四条隔离规则保证两类检索互不可见。对象定义在
# ``common/type_def/filter.py``（消费方横跨 api 与 construction，构建层不依赖
# control），此处再导出——规约的引用入口仍是本模块：
#
# 1. 检索与列举：接口层对默认 profile 的请求追加
#    ``system_metadata.memory_type NOT_IN`` 谓词、对路由值在集合内的请求追加等值
#    谓词（``api/memory_api_impl/query_ops.py``）；
# 2. 去重召回：按候选的 ``memory_type`` 加过滤条件（``construction/dedup.py`` 与
#    ``dedup_impl/`` 两实现）——对话记忆与经验不互判为重复；
# 3. 经验条目只对编辑者可读写（``api/memory_api_impl/pep_ops.py``）；
# 4. 类型不能伪造：``add`` 不接受 ``skill``，``update`` 不能改入/改出这两种取值
#    （``query_ops.update``、``cloud_engine._require_profile``）。
__all__ = [
    "ISOLATED_MEMORY_TYPES",
    "NO_FALLBACK_MEMORY_TYPES",
    "MemoryPipeline",
    "PipelineBinding",
    "PipelineProducer",
    "require_experience_profile",
    "route_value_from_query",
    "route_value_of",
]


def route_value_of(
    extensions: Mapping[str, Any] | None,
    filters: FilterExpr | None,
    route_key: str,
) -> str:
    """按 profile 选择同一口径解析路由值：``extensions[route_key]`` 优先，其次取
    规范化 FilterExpr 中逻辑上强制成立的 ``system_metadata.<route_key>`` 唯一等值
    （``memory_type`` 裸字段仅作兼容别名）。

    接口层的类型隔离谓词（mem2.0 §2.3）用它取「本次请求选 profile 所依据的值」：
    谓词与 profile 选择若各解析一套，同一个请求会一边选了经验 profile、一边追加
    排除经验的系统谓词。实现住在这里与 :class:`MetadataPipeline` 共用一份，
    ``pipeline_impl/metadata_pipeline.py`` 不留副本。
    """
    if route_key != "memory_type":
        memory_type = route_value_of(extensions, filters, "memory_type")
        if memory_type in ISOLATED_MEMORY_TYPES:
            return memory_type
    value = str((extensions or {}).get(route_key, "")).strip()
    if value:
        return value
    # filters 须已规范化（RetrievalQuery / list 边界各自完成）；memory_type 使用
    # 共享规范字段常量。
    filter_field = (
        MEMORY_TYPE_FILTER_FIELD
        if route_key == "memory_type"
        else f"system_metadata.{route_key}"
    )
    routed = extract_required_equality(filters, filter_field)
    return str(routed).strip() if routed is not None else ""


def route_value_from_query(query: RetrievalQuery, route_key: str) -> str:
    """查询路由保留隔离类型专用 profile，其余请求使用配置的路由键。"""
    return route_value_of(query.extensions, query.filters, route_key)


@dataclass(frozen=True)
class PipelineBinding:
    """一个 pipeline profile 绑定的一组跨层算子。"""

    name: str
    index_builder: IndexBuilder
    retriever: Retriever
    evolver: Evolver
    classifier: Classifier | None = None


class MemoryPipeline(ControlOperator):
    """按写入/查询上下文选择 pipeline profile。"""

    @property
    def route_key(self) -> str:
        """用于接收空间策略绑定的查询路由键。"""
        return "memory_type"

    def explicit_write_binding(
        self, metadata: Mapping[str, MetadataValueType],
    ) -> PipelineBinding | None:
        """Resolve an explicit write route without a fallback profile."""
        return None

    @abstractmethod
    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        """根据本次写入产生的 MemoryUnit 选择构建侧 profile。"""

    @abstractmethod
    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        """根据 RetrievalQuery 选择查询侧 profile。"""

    def has_profile_for(self, memory_type: str) -> bool:
        """该 ``memory_type`` 取值是否有对应的 profile（不回落语义的查询面）。

        引擎在选 profile 之前用它与 :data:`NO_FALLBACK_MEMORY_TYPES` 核对：不回落的
        取值没有 profile 即返回参数错误，而不是静默落默认 profile（§2.1）。
        """
        return False


def require_experience_profile(
    pipeline: MemoryPipeline | None,
    value: str,
    *,
    infer: bool | None = None,
    procedural: bool = False,
    middle: bool = False,
) -> None:
    """Enforce the experience profile and keep skill writes internal."""
    if value == "skill":
        if infer is not None:
            raise ValidationError(
                "memory_type='skill' 不能经 add 写入：技能由技能归纳算子从多条"
                "独立复现的经验提炼（§2.5），不经写入接口"
            )
        return
    if value not in NO_FALLBACK_MEMORY_TYPES:
        return
    if pipeline is None or not pipeline.has_profile_for(value):
        raise ValidationError(
            f"memory_type={value!r} 须由对应的流水线 profile 处理："
            "部署未启用流水线或未为它配置 profile，拒绝执行"
        )
    if infer is not None:
        # 拆成两层 if（G.CTL.03 单个 if 至多 3 个布尔表达式）：
        # 外层判「带 infer 传参」，内层判三种非法形态。
        if not infer or procedural or middle:
            raise ValidationError(
                f"memory_type={value!r} 只支持同步派生写入（infer=true），"
                "且不得带 procedural=true 或 middle=true"
            )
