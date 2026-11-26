# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""IndexBuilder — 多形式索引构建（架构 §6.2）。

在各粒度记忆之上构建/更新索引（文档/关键词/向量/图，按配置启用）。
索引是可配置的检索结构、并非记忆固有结构，全部可从真源重建。
本算子负责构建**逻辑**（调用 Chunker/Tokenizer/Embedder 等共享插件
生成索引投影），持久化由注入的 ``jiuwen_memory/storage`` 后端承担——构建与
存储经此解耦。构建各索引记录时把来源 ``MemoryUnit.scope`` 落到记录的
专用 ``scope`` 字段（``VectorRecord``/``Document``/``Node``/``FusionRecord``
等），使检索得以按 scope 原生隔离。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from .base import ConstructionOperator, OperatorType

logger = get_logger(__name__)

if TYPE_CHECKING:
    from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
    from jiuwen_memory.storage.domain_store import DomainStore


class UnitCommitState(str, Enum):
    """批量建索引时单条记忆的落盘结论（F10 决策 4）。

    - ``COMMITTED``：正排已确认写入；检索索引失败时该条仍为 COMMITTED，但带 ``error``
      （部分提交——本体在、索引可重建）；
    - ``FAILED``：本批更早的条目失败后被跳过，**未尝试写入**，可安全重试；
    - ``UNKNOWN``：写入过程中抛错，无法确认是否落盘，重试前须先核对。
    """

    COMMITTED = "committed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnitCommitStatus:
    """``build_grouped`` 的逐条结果；与输入 ``units`` 按下标对齐。"""

    unit_id: str
    state: UnitCommitState
    error: Exception | None = None


def align_unit_receipts(
    units: list[MemoryUnit], statuses: list[UnitCommitStatus]
) -> list[UnitCommitStatus]:
    """把 delegate 回执对齐给输入，返回与输入等长、顺序一致的列表。

    S05 规定 unit id 只在 scope 内唯一，跨 scope 同名 id 是两条合法记录。规则：

    - **回执数量与输入一致**：按位置使用（delegate 的逐条顺序契约）；位置上的 id
      不一致时不猜，退回唯一 id 匹配。
    - **数量不一致**（漏回/多回）：只按 id 唯一匹配；同一 id 在多个位置出现时无法
      判断回执属于谁，这些位置一律 ``UNKNOWN``，绝不把一份 ``COMMITTED`` 挪给别的项。
    """
    if len(statuses) == len(units) and all(
        status.unit_id == unit.id for status, unit in zip(statuses, units)
    ):
        return list(statuses)

    positions_by_id: dict[str, list[int]] = {}
    for position, unit in enumerate(units):
        positions_by_id.setdefault(unit.id, []).append(position)
    receipt_counts: dict[str, int] = {}
    for status in statuses:
        receipt_counts[status.unit_id] = receipt_counts.get(status.unit_id, 0) + 1

    assigned: dict[int, UnitCommitStatus] = {}
    ambiguous: set[int] = set()
    for status in statuses:
        candidates = positions_by_id.get(status.unit_id, [])
        if receipt_counts[status.unit_id] > 1:
            # 同一 id 返回了多份回执且彼此矛盾：无法判断哪份可信，全部按 UNKNOWN。
            ambiguous.update(candidates)
            continue
        candidates = [position for position in candidates if position not in assigned]
        if len(candidates) == 1:
            assigned[candidates[0]] = status
        elif len(candidates) > 1:
            ambiguous.update(candidates)

    if len(assigned) != len(units) or len(statuses) != len(units):
        logger.warning(
            "align_unit_receipts: receipt mismatch "
            "(units=%d matched=%d receipts=%d ambiguous=%d)",
            len(units),
            len(assigned),
            len(statuses),
            len(ambiguous),
        )
    aligned: list[UnitCommitStatus] = []
    for position, unit in enumerate(units):
        status = assigned.get(position)
        if status is not None:
            aligned.append(status)
        elif position in ambiguous:
            aligned.append(
                UnitCommitStatus(
                    unit_id=unit.id,
                    state=UnitCommitState.UNKNOWN,
                    error=RuntimeError("ambiguous unit id in receipt alignment"),
                )
            )
        else:
            aligned.append(
                UnitCommitStatus(
                    unit_id=unit.id,
                    state=UnitCommitState.UNKNOWN,
                    error=RuntimeError("index builder returned no receipt for unit"),
                )
            )
    return aligned


def _group_positions_by_scope(
    units: list[MemoryUnit], positions: list[int]
) -> dict[tuple, list[int]]:
    """按 unit 所在 scope 分组，但保留原始输入位置。"""
    groups: dict[tuple, list[int]] = {}
    for position in positions:
        scope = units[position].scope
        key = (scope.org, scope.space, scope.user, scope.agent, scope.session)
        groups.setdefault(key, []).append(position)
    return groups


def _record_index_error(
    positions: list[int],
    statuses: list[UnitCommitStatus],
    exc: Exception,
) -> None:
    """把索引维护错误附到回执：已确认的正排保持 COMMITTED，但不再是完整成功。"""

    for position in positions:
        current = statuses[position]
        # 每条回执单独包装：delegate 可能把同一个异常实例放进多条回执，直接挂载会让
        # 后处理（add_note 等）串到别的 unit 上。已有错误时生成复合错误，保留原链。
        if current.error is None:
            wrapped = RuntimeError(f"{type(exc).__name__}: {exc}")
            wrapped.__cause__ = exc
        else:
            wrapped = RuntimeError(
                f"{current.error}；{type(exc).__name__}: {exc}"
            )
            wrapped.__cause__ = current.error
        statuses[position] = replace(current, error=wrapped)


class IndexBuilderProducer(Factory):
    """IndexBuilder 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``index_builder_impl`` 下
    以 ``@IndexBuilderProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "constructor"

    @classmethod
    def build(
        cls,
        target: str,
        params: Any,
        ctx: Any,
        *,
        name: str = "",
    ) -> IndexBuilder:
        """Build one target and opt in to Schema Property index maintenance.

        Wrapping at the producer boundary keeps the derived index independent
        of a concrete ``hybrid``/``unified`` implementation. Consequently a
        deployment may switch or mix registered IndexBuilder targets without
        silently losing Entity-to-Property maintenance. With the global flag
        disabled the original builder is returned untouched and no KV port is
        resolved or accessed.
        """

        builder = super().build(target, params, ctx, name=name)
        from jiuwen_memory.config.context import ComponentConfig

        config = ComponentConfig(params=dict(params or {}), ctx=ctx, target=target, name=name)
        from jiuwen_memory.common.security.privacy.base import optional_privacy
        from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder

        privacy = optional_privacy(config)
        if not as_bool(config.get("schema_enabled"), default=False):
            return PrivacyIndexBuilder(builder, privacy) if privacy else builder

        from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
        from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

        if isinstance(builder, SchemaPropertyIndexingBuilder):
            return PrivacyIndexBuilder(builder, privacy) if privacy else builder
        manager = StoreManagerProducer.resolve(config)
        kv_name = resolve_name(config, "kv_store")
        if not manager.has_kv(kv_name):
            # A unified/custom data plane may intentionally expose no KV port.
            # Retrieval then uses its compatible DomainStore scope fallback.
            return PrivacyIndexBuilder(builder, privacy) if privacy else builder
        builder = SchemaPropertyIndexingBuilder(
            builder,
            SchemaPropertyIndex(manager.kv(kv_name)),
        )
        return PrivacyIndexBuilder(builder, privacy) if privacy else builder


class IndexBuilder(ConstructionOperator):
    """索引构建的统一入口——记忆写入只经本算子，调用方不直接调 Storage 写接口。

    写接口（``build``/``update``）用 ``IndexWriteMode`` 表达写入范围：

    - ``ALL``（默认）——**交付记忆**：正排（记忆本体）与全部检索索引均写入；
    - ``FORWARD_ONLY`` ——**仅正排**：只回写记忆本体，检索索引不动。用于生命周期
      治理（归档/遗忘时真源保留新状态、检索索引另行处置）；
    - ``RETRIEVAL_ONLY`` ——**仅检索索引**：记忆本体保持不动。用于索引迁移
      （记忆不变、检索索引换承载者）与部分失败后的补建。

    删除接口（``remove``）用 ``IndexRemoveMode`` 表达删除语义：

    - ``HARD``（默认）——**硬删除**：检索索引与记忆本体一并物理删除；
    - ``SOFT`` ——**软删除**：只移出检索索引（search/recall 不再召回），记忆本体
      保留，``get``/``list`` 仍可读。

    不支持细粒度控制的实现（如全权委托 Storage 的 ``unified``）把枚举原样下传，
    能否拆分由该 Storage 实现按自身能力决定。
    """

    def read_domain_store(self) -> DomainStore | None:
        """Return the write-bound truth reader, if this builder delegates to a DomainStore.

        A missing capability must be paired with an explicitly configured/injected
        DomainStore. Evolvers never infer the truth reader from a default data plane.
        """
        return None

    def supports_grouped_build(self) -> bool:
        """是否支持批量建索引并逐条报告落盘结论（F10 决策 4）。

        默认 ``False``：不声明支持时上层退回逐条 ``build``，不会因为配置了合并路径
        而假定所有构建器都能给出逐条结果（S09 不变量 6、14）。
        """
        return False

    def build_grouped(self, units: list[MemoryUnit]) -> list[UnitCommitStatus]:
        """批量建索引，逐条返回落盘结论；**不抛异常**，部分失败经返回值体现。

        默认实现明确不支持——调用方必须先查 :meth:`supports_grouped_build`，
        不能把「不支持」当成空结果（S09 不变量 12）。
        """
        raise UnsupportedCapabilityError(
            "grouped_build",
            type(self).__name__,
            "IndexBuilder",
            f"{type(self).__name__} does not support grouped build",
        )

    @abstractmethod
    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        """为一批记忆单元构建已启用的各形式索引。

        ``mode=RETRIEVAL_ONLY`` 时记忆本体已存在，只补建检索索引；
        ``mode=FORWARD_ONLY`` 时只交付记忆本体，不建检索索引。
        """

    @abstractmethod
    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """记忆变更后增量更新对应索引条目（含记忆本体的回写）。

        ``mode=FORWARD_ONLY`` 时只回写记忆本体，检索索引不动——供上层表达「本体改状态、
        但检索索引另行处置」（如遗忘：回写 FORGOTTEN 后再 ``remove(mode=SOFT)``
        移出检索），避免先重建检索索引再删掉那一轮无用功。
        """

    @abstractmethod
    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """删除一批记忆单元对应的索引条目（幂等）。

        ``mode=SOFT`` 为软删除：只移出检索索引（search/recall 不再召回），记忆本体
        保留，``get``/``list`` 仍可读；``mode=HARD`` 为硬删除：检索索引与记忆本体
        一并物理删除。
        """

    @abstractmethod
    def rebuild(self) -> None:
        """从真源全量重建索引（删索引不丢数据的保障）。"""

    def projection_graph(self):
        """Return the injected graph dependency for graph-only projection, otherwise None."""
        return None

    def supported_write_modes(self) -> set[IndexWriteMode]:
        """公开能力：声明本构建器实现的写入模式（S05）。

        完整写入链的构建器必须声明 :data:`~storage.types.IndexWriteMode.ALL`（以及
        :data:`~storage.types.IndexWriteMode.FORWARD_ONLY` 用于生命周期治理）；只声明
        :data:`~storage.types.IndexWriteMode.RETRIEVAL_ONLY` 的构建器（如关系投影器）
        只能被配置到投影位置，配到主写入链时装配期拒绝。装配器按此能力声明判断，不按
        target 名、模块路径或具体类型识别（S09 §9）。
        """
        return {
            IndexWriteMode.ALL,
            IndexWriteMode.FORWARD_ONLY,
            IndexWriteMode.RETRIEVAL_ONLY,
        }


def require_full_write_chain(builder: IndexBuilder) -> IndexBuilder:
    """装配期守卫：主写入链构建器必须声明完整的正排/检索写入能力。

    只声明 RETRIEVAL_ONLY 的构建器（如 ``evidence_graph`` 关系投影器）配到 Engine 主
    写入链位置时在此拒绝——判断依据是 :meth:`IndexBuilder.supported_write_modes` 能力
    声明，不按 target 名或具体类型识别（S05 / S09 §9）。
    """
    from jiuwen_memory.common.errors import ValidationError

    modes = builder.supported_write_modes()
    if IndexWriteMode.ALL not in modes:
        raise ValidationError(
            "main write chain requires an IndexBuilder that supports ALL write mode; "
            f"{type(builder).__name__} declares {sorted(m.value for m in modes)}"
        )
    return builder


class SchemaPropertyIndexingBuilder(IndexBuilder):
    """Target-neutral decorator for the Schema Property reverse index."""

    def __init__(
        self,
        delegate: IndexBuilder,
        property_index: SchemaPropertyIndex,
    ) -> None:
        self._delegate = delegate
        self._property_index = property_index

    def read_domain_store(self) -> DomainStore | None:
        return self._delegate.read_domain_store()

    @property
    def delegate(self) -> IndexBuilder:
        """Return the wrapped builder for diagnostics and focused tests."""

        return self._delegate

    def operator_type(self) -> OperatorType:
        return self._delegate.operator_type()

    def health(self) -> None:
        self._delegate.health()

    def supports_grouped_build(self) -> bool:
        return self._delegate.supports_grouped_build()

    def build_grouped(self, units: list[MemoryUnit]) -> list[UnitCommitStatus]:
        if not self._delegate.supports_grouped_build():
            # 保持「不支持」原始异常语义，不把能力缺失当成写入失败去失效反向索引。
            return self._delegate.build_grouped(units)
        try:
            raw_statuses = self._delegate.build_grouped(units)
        except Exception as exc:
            self._invalidate_after_failure(units, exc)
            raise
        statuses = align_unit_receipts(units, raw_statuses)
        committed_positions = [
            position
            for position, status in enumerate(statuses)
            if status.state is UnitCommitState.COMMITTED
        ]
        unknown_positions = [
            position
            for position, status in enumerate(statuses)
            if status.state is UnitCommitState.UNKNOWN
        ]
        if committed_positions:
            # 批量路径的 COMMITTED 表示「已确认正排提交」（检索索引仍可能附带 error），
            # 与逐条 build(mode=ALL) 的成员语义一致：反向索引记录的是真源成员关系，
            # 因此必须 upsert（加成员）而不是 mark_forward_only（排除出反向索引）。
            # 反向索引是派生数据：维护失败不能吞掉 delegate 已经给出的提交回执，
            # 否则调用方重试会把已写入的条目再提交一次。失败时把索引维护错误附到回执上
            # （保持 COMMITTED，表示部分成功），并按 scope 独立失效让读侧回退真源。
            for positions in _group_positions_by_scope(units, committed_positions).values():
                scoped = [units[position] for position in positions]
                try:
                    self._property_index.upsert(scoped)
                except Exception as exc:  # noqa: BLE001 —— 派生索引失败不得改写正排状态
                    logger.exception(
                        "SchemaPropertyIndexingBuilder: reverse index upsert failed "
                        "for %d committed units",
                        len(scoped),
                    )
                    _record_index_error(positions, statuses, exc)
                    self._invalidate_scope_quietly(
                        scoped[0].scope, positions, statuses
                    )
        if unknown_positions:
            # UNKNOWN 的正排可能已经落盘，但这次没有进入反向索引：让相关 scope 失效，
            # 查询回退到真源核对，避免把缺成员的旧索引当成完整结果。
            for positions in _group_positions_by_scope(units, unknown_positions).values():
                self._invalidate_scope_quietly(
                    units[positions[0]].scope, positions, statuses
                )
        return statuses

    def _invalidate_scope_quietly(
        self,
        scope: Scope,
        positions: list[int],
        statuses: list[UnitCommitStatus],
    ) -> None:
        """按 scope 独立失效；失败只记日志并附到回执，不影响其他 scope 与写入结论。"""

        try:
            self._property_index.invalidate_scope(scope)
        except Exception as exc:  # noqa: BLE001 —— 失效本身失败只能降级为可观测日志/回执
            logger.exception(
                "SchemaPropertyIndexingBuilder: scope invalidation failed"
            )
            _record_index_error(positions, statuses, exc)

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        try:
            self._delegate.build(units, mode=mode)
        except Exception as exc:
            self._invalidate_after_failure(units, exc)
            raise
        if mode is IndexWriteMode.FORWARD_ONLY:
            self._property_index.mark_forward_only(units)
        else:
            self._property_index.upsert(units)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        try:
            self._delegate.update(units, mode=mode)
        except Exception as exc:
            self._invalidate_after_failure(units, exc)
            raise
        # FORWARD_ONLY update deliberately leaves every retrieval projection
        # untouched. Lifecycle governance relies on that retained membership
        # to hydrate archived/superseded history after its later SOFT remove.
        if mode is not IndexWriteMode.FORWARD_ONLY:
            self._property_index.upsert(units)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        try:
            self._delegate.remove(units, mode=mode)
        except Exception as exc:
            self._invalidate_after_failure(units, exc)
            raise
        # Archived/superseded truth remains available to knowledge-time
        # history. Only physical deletion removes it from the reverse index.
        if mode is IndexRemoveMode.HARD:
            self._property_index.remove(units)

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        """Delegate rollback cleanup and remove reverse memberships as well."""

        remover = getattr(self._delegate, "remove_with_scope", None)
        if not callable(remover):
            raise AttributeError(
                f"{type(self._delegate).__name__} does not support remove_with_scope"
            )
        try:
            remover(unit_ids, scope)
        except Exception as exc:
            self._invalidate_scope_after_failure(scope, exc)
            raise
        self._property_index.remove_with_scope(scope, unit_ids)

    def rebuild(self) -> None:
        self._delegate.rebuild()

    def projection_graph(self):
        return self._delegate.projection_graph()

    def supported_write_modes(self) -> set[IndexWriteMode]:
        return self._delegate.supported_write_modes()

    def _invalidate_after_failure(self, units: list[MemoryUnit], original: Exception) -> None:
        try:
            self._property_index.invalidate(units)
        except Exception as cleanup_error:
            original.add_note(
                "Schema Property reverse-index invalidation also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    def _invalidate_scope_after_failure(self, scope: Scope, original: Exception) -> None:
        try:
            self._property_index.invalidate_scope(scope)
        except Exception as cleanup_error:
            original.add_note(
                "Schema Property scope invalidation also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
