"""ElasticsearchEntityStore 的 Scope 隔离序列化回归测试。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.type_def import (
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    Scope,
)
from jiuwen_memory.common.type_def.scope import space_id_from_scope
from jiuwen_memory.storage.entity_impl.elasticsearch_entity_store import ElasticsearchEntityStore
from jiuwen_memory.storage.entity_store import bind_entity_operations_to_scope

pytestmark = pytest.mark.unit


class RecordingClient:
    def __init__(self) -> None:
        self.bulk_calls: list[dict[str, Any]] = []

    def bulk(self, **kwargs: Any) -> dict[str, Any]:
        self.bulk_calls.append(kwargs)
        actions = kwargs["operations"]
        return {
            "items": [
                {next(iter(action)): {"_id": action[next(iter(action))]["_id"], "status": 201}}
                for action in actions[::2]
            ]
        }


def _store(client: RecordingClient) -> ElasticsearchEntityStore:
    store = ElasticsearchEntityStore(hosts="http://unused")
    setattr(store, "_client", client)
    setattr(store, "_index_ready", True)
    return store


def _record(scope: Scope, entity_id: str = "entity-1") -> EntityRecord:
    del scope
    return EntityRecord(
        id=entity_id,
        entity_text="Alice",
        entity_text_hash="hash",
        entity_type="PROPER",
        linked_memory_ids=("memory-1",),
    )


def test_bulk_document_id_binds_full_scope_and_returns_logical_id() -> None:
    client = RecordingClient()
    store = _store(client)
    alice_scope = Scope(org="acme", space="shared", user="alice", agent="a", session="s")
    bob_scope = Scope(org="acme", space="shared", user="bob", agent="a", session="s")

    alice_result = store.execute_operations(
        alice_scope,
        [EntityOperation(type=EntityOpType.INSERT, record=_record(alice_scope))],
    )
    bob_result = store.execute_operations(
        bob_scope,
        [EntityOperation(type=EntityOpType.INSERT, record=_record(bob_scope))],
    )

    alice_action, alice_document = client.bulk_calls[0]["operations"]
    bob_action, bob_document = client.bulk_calls[1]["operations"]
    alice_id = alice_action["index"]["_id"]
    bob_id = bob_action["index"]["_id"]

    assert alice_id != bob_id, "同 space 下不同 Scope 的逻辑同 ID 必须映射为不同 ES 文档 ID"
    assert alice_document["entity_id"] == bob_document["entity_id"] == "entity-1"
    assert alice_document["space_id"] == space_id_from_scope(alice_scope)
    assert bob_document["space_id"] == space_id_from_scope(bob_scope)
    assert alice_document["user"] == "alice"
    assert bob_document["user"] == "bob"
    assert alice_result.successful_ids == bob_result.successful_ids == ["entity-1"]


def test_hit_restores_logical_entity_id_from_scoped_document() -> None:
    scope = Scope(org="acme", space="shared", user="alice", agent="a", session="s")
    record = _record(scope)
    bound_record = bind_entity_operations_to_scope(
        scope,
        [EntityOperation(type=EntityOpType.INSERT, record=record)],
    )[0].record
    assert bound_record is not None

    to_document = getattr(ElasticsearchEntityStore, "_to_document")
    hit_to_record = getattr(ElasticsearchEntityStore, "_hit_to_entity_record")
    restored = hit_to_record(
        {
            "_id": "scope-v1-opaque-physical-id",
            "_source": {
                **to_document(bound_record),
                "entity_id": record.id,
            },
        }
    )

    assert restored.id == record.id
    assert restored.filters == EntityStoreFilters.from_scope(scope)


def test_existing_index_without_entity_id_requires_rebuild() -> None:
    class ExistingIndexClient:
        class Indices:
            @staticmethod
            def get_mapping(**kwargs: Any) -> dict[str, Any]:
                keyword_fields = (
                    "entity_text_hash",
                    "org",
                    "space",
                    "user",
                    "agent",
                    "session",
                )
                return {
                    "entities": {
                        "mappings": {
                            "_routing": {"required": True},
                            "properties": {
                                name: {"type": "keyword"} for name in keyword_fields
                            },
                        }
                    }
                }

        indices = Indices()

    store = ElasticsearchEntityStore(hosts="http://unused", index="entities")

    with pytest.raises(BackendError, match="entity_id"):
        getattr(store, "_validate_existing_index")(ExistingIndexClient())
