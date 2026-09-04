# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityStore — 实体反向索引存储端口（与 VectorStore/FulltextStore 平级）。

抽象实体索引的读写能力：hash 精确查询、bulk 变更、反查关联。实现侧
（``ElasticsearchEntityStore``）走 Elasticsearch，后续可换其他后端。

迁移自原 ``core.ports.entity_store``，签名做两处改造：
- ``space_id`` 从 ``UUID`` 改 ``str``（当前工程 routing 不要求 UUID，存
  ``space_id_from_scope`` 的 str 算值）
- ``find_by_linked_memory_id`` 的 ``memory_id`` 从 ``UUID`` 改 ``str``（存 unit.id）

入参与 ``BaseStore`` 对齐，Entity 端口以完整 ``Scope`` 作为每个方法的首参。
后端可将 ``scope.space`` 映射为 routing/namespace，同时必须保留
``org/space/user/agent/session`` 五维硬隔离；兼容旧 ``space_id + filters`` 后端的
转换仅在 Storage 内部完成。

**2026-08-12 改造**：归并退化为 hash 精确 only，砍掉向量 kNN 检索能力。
``search``（向量 kNN）方法删除，entity 索引不再存向量、不再依赖 Embedder。
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import replace
from inspect import Parameter, signature
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
)
from jiuwen_memory.common.type_def.scope import Scope, space_id_from_scope

from .base import BaseStore

_LEGACY_SCOPED_ID_PREFIX = "scope-v1:"


def scoped_entity_document_id(scope: Scope, entity_id: str) -> str:
    """Return a bounded physical ID unique to one logical entity Scope.

    Elasticsearch routes all records in a space to one shard.  Its document ID
    must therefore include every remaining Scope coordinate as well; routing
    alone cannot stop an update or delete from targeting another user in the
    same space.
    """
    payload = _scope_id_payload(scope, entity_id)
    return "scope-v1-" + hashlib.sha256(payload).hexdigest()


def _scope_id_payload(scope: Scope, entity_id: str) -> bytes:
    return json.dumps(
        [scope.org, scope.space, scope.user, scope.agent, scope.session, entity_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()


def _legacy_scoped_entity_id(scope: Scope, entity_id: str) -> str:
    """Make legacy ``space_id`` backends safe without changing their schema."""
    encoded = urlsafe_b64encode(_scope_id_payload(scope, entity_id)).decode().rstrip("=")
    return f"{_LEGACY_SCOPED_ID_PREFIX}{encoded}"


def _logical_legacy_entity_id(scope: Scope, stored_id: str) -> str | None:
    """Decode a legacy physical ID, returning ``None`` for another Scope."""
    if not stored_id.startswith(_LEGACY_SCOPED_ID_PREFIX):
        return stored_id
    encoded = stored_id.removeprefix(_LEGACY_SCOPED_ID_PREFIX)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        org, space, user, agent, session, entity_id = json.loads(urlsafe_b64decode(padded))
    except (TypeError, ValueError):
        return None
    if (org, space, user, agent, session) != _scope_coordinates(scope):
        return None
    return entity_id if isinstance(entity_id, str) else None


def _scope_coordinates(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def bind_entity_operations_to_scope(
    scope: Scope,
    operations: list[EntityOperation],
) -> list[EntityOperation]:
    """Bind optional legacy record projections to the explicit EntityStore Scope.

    ``EntityRecord`` is a domain payload, so callers do not derive backend routing
    or filter fields.  Older stores still require those fields on the record;
    materialize them only at this Storage boundary and reject a caller-provided
    projection that disagrees with the authoritative Scope.
    """
    expected_space_id = space_id_from_scope(scope)
    expected_filters = EntityStoreFilters.from_scope(scope)
    bound_operations: list[EntityOperation] = []
    for operation in operations:
        if operation.type not in {EntityOpType.INSERT, EntityOpType.UNLINK_UPDATE}:
            bound_operations.append(operation)
            continue
        record = operation.record
        if record is None:
            bound_operations.append(operation)
            continue
        if record.space_id is not None and record.space_id != expected_space_id:
            raise ValidationError(
                f"EntityRecord namespace differs from explicit scope: {record.id}"
            )
        if record.filters is not None and record.filters != expected_filters:
            raise ValidationError(
                f"EntityRecord namespace differs from explicit scope: {record.id}"
            )
        bound_record = replace(
            record,
            space_id=expected_space_id,
            filters=expected_filters,
        )
        bound_operations.append(replace(operation, record=bound_record))
    return bound_operations


def validate_entity_operations_scope(scope: Scope, operations: list[EntityOperation]) -> None:
    """Validate record projections against Scope without exposing binding details."""
    bind_entity_operations_to_scope(scope, operations)


class EntityStoreProducer(Factory):
    """EntityStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 elasticsearch）。各实现在 ``entity_impl`` 下以
    ``@EntityStoreProducer.register("<后端>")`` 自注册——注册发生在 import
    实现模块时，由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "entity_store"


class EntityStore(BaseStore):
    """实体反向索引存储抽象。

    新契约的首参统一为完整 ``Scope``。后端可以把 ``scope.space`` 映射为
    routing/namespace，同时保留 org、user、agent、session 作为硬隔离字段。
    ``ScopedEntityStore`` 为旧的 ``space_id + filters`` 实现提供兼容适配。
    """

    @abstractmethod
    def ensure_index(self) -> None:
        """确保索引已创建并就绪。使用前必须调一次，否则后续查询抛 not ready。"""

    @abstractmethod
    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        limit: int = 500,
    ) -> list[EntityRecord]:
        """按 entity_text_hash keyword term 查询，返回命中的实体记录。"""

    @abstractmethod
    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
    ) -> list[EntityRecord]:
        """反查：哪些实体关联了该 memory_id（unlink 用）。

        filters 复用写入侧的 actor_id 隔离维度——unlink 只命中调用方 scope
        所属的实体文档，避免 space 内跨 user 的孤立误删（纵深防御：当前
        unit.id 是 UUID4 全局唯一不会撞，但把隔离下沉到存储层后，即便未来
        出现非 UUID 的 id 路径也安全）。
        """

    @abstractmethod
    def execute_operations(
        self,
        scope: Scope,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        """bulk 变更（INSERT/LINK/UNLINK_UPDATE/DELETE 混合），per-item 粒度返回。"""


class ScopedEntityStore(EntityStore):
    """把新 Scope 端口适配到新旧两种 EntityStore 后端。

    旧实现按首参名称 ``space_id`` 并要求 ``filters``；新实现按首参名称
    ``scope`` 并在后端内部从 Scope 派生 namespace。适配判断只看签名，不用
    捕获业务 ``TypeError``，避免把后端真实错误误判成旧接口。
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def security(self):
        return self._backend.security

    def store_type(self):
        return self._backend.store_type()

    def health(self) -> None:
        return self._backend.health()

    def ensure_index(self) -> None:
        return self._backend.ensure_index()

    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        limit: int = 500,
    ) -> list[EntityRecord]:
        method = self._backend.find_by_entity_text_hash
        if _takes_scope(method):
            return method(scope, entity_text_hashes, limit=limit)
        records = method(
            space_id_from_scope(scope),
            entity_text_hashes,
            filters=EntityStoreFilters.from_scope(scope),
            limit=limit,
        )
        return _restore_legacy_records(scope, records)

    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
    ) -> list[EntityRecord]:
        method = self._backend.find_by_linked_memory_id
        if _takes_scope(method):
            return method(scope, memory_id)
        records = method(
            space_id_from_scope(scope),
            memory_id,
            filters=EntityStoreFilters.from_scope(scope),
        )
        return _restore_legacy_records(scope, records)

    def execute_operations(
        self,
        scope: Scope,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        bound_operations = bind_entity_operations_to_scope(scope, operations)
        method = self._backend.execute_operations
        if _takes_scope(method):
            return method(scope, bound_operations)
        translated, logical_ids = _translate_legacy_operations(scope, bound_operations)
        result = method(space_id_from_scope(scope), translated)
        return EntityBatchResult(
            successful_ids=[
                logical_ids.get(record_id, record_id) for record_id in result.successful_ids
            ],
            failed_ids=[logical_ids.get(record_id, record_id) for record_id in result.failed_ids],
        )


def adapt_entity_store(candidate: Any) -> EntityStore:
    """把旧 EntityStore 实现转换成统一 Scope 端口。"""
    # CompositeStorage proxies an already-adapted ScopedEntityStore.  Preserve
    # that proxy so its StorageSecurity check continues to see the real Scope.
    if isinstance(candidate, ScopedEntityStore) or isinstance(
        getattr(candidate, "_store", None), ScopedEntityStore
    ):
        return candidate
    return ScopedEntityStore(candidate)


def _takes_scope(method: Any) -> bool:
    """判断绑定方法是否已经采用新 ``scope`` 首参。"""
    try:
        params = list(signature(method).parameters.values())
    except (TypeError, ValueError):
        return False
    first = next(
        (
            param
            for param in params
            if param.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ),
        None,
    )
    return first is not None and first.name in {"scope", "target_scope"}


def _restore_legacy_records(scope: Scope, records: list[EntityRecord]) -> list[EntityRecord]:
    expected_filters = EntityStoreFilters.from_scope(scope)
    restored: list[EntityRecord] = []
    for record in records:
        if record.filters != expected_filters:
            continue
        logical_id = _logical_legacy_entity_id(scope, record.id)
        if logical_id is not None:
            restored.append(replace(record, id=logical_id))
    return restored


def _translate_legacy_operations(
    scope: Scope,
    operations: list[EntityOperation],
) -> tuple[list[EntityOperation], dict[str, str]]:
    translated: list[EntityOperation] = []
    logical_ids: dict[str, str] = {}
    for operation in operations:
        if operation.record is not None:
            stored_id = _legacy_scoped_entity_id(scope, operation.record.id)
            logical_ids[stored_id] = operation.record.id
            translated.append(replace(operation, record=replace(operation.record, id=stored_id)))
        elif operation.record_id is not None:
            stored_id = _legacy_scoped_entity_id(scope, operation.record_id)
            logical_ids[stored_id] = operation.record_id
            translated.append(replace(operation, record_id=stored_id))
        else:
            translated.append(operation)
    return translated, logical_ids
