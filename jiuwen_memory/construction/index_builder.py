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
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from .base import ConstructionOperator, OperatorType

if TYPE_CHECKING:
    from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex


class IndexBuilderProducer(Factory):
    """IndexBuilder 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``index_builder_impl`` 下以
    ``@IndexBuilderProducer.register("<名>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`construction.bootstrap.register_constructors` 统一触发。
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
        if not as_bool(config.get("schema_enabled"), default=False):
            return builder

        from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
        from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

        if isinstance(builder, SchemaPropertyIndexingBuilder):
            return builder
        manager = StoreManagerProducer.resolve(config)
        kv_name = resolve_name(config, "kv_store")
        if not manager.has_kv(kv_name):
            # A unified/custom data plane may intentionally expose no KV port.
            # Retrieval then uses its compatible DomainStore scope fallback.
            return builder
        return SchemaPropertyIndexingBuilder(
            builder,
            SchemaPropertyIndex(manager.kv(kv_name)),
        )


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


class SchemaPropertyIndexingBuilder(IndexBuilder):
    """Target-neutral decorator for the Schema Property reverse index."""

    def __init__(
        self,
        delegate: IndexBuilder,
        property_index: SchemaPropertyIndex,
    ) -> None:
        self._delegate = delegate
        self._property_index = property_index

    @property
    def delegate(self) -> IndexBuilder:
        """Return the wrapped builder for diagnostics and focused tests."""

        return self._delegate

    def operator_type(self) -> OperatorType:
        return self._delegate.operator_type()

    def health(self) -> None:
        self._delegate.health()

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
