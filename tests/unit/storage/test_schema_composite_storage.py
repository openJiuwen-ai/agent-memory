"""Schema CompositeStorage configuration and physical port isolation."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.schema_bootstrap import register_schema_storage
from jiuwen_memory.storage.storage import StorageProducer
from jiuwen_memory.storage.storage_impl import CompositeStorage
from jiuwen_memory.storage.types import Document, VectorRecord

pytestmark = pytest.mark.unit


def _schema_storage() -> CompositeStorage:
    register_plugins()
    register_backends()
    register_schema_storage()
    overlay = AssemblyContext.from_dict(
        {
            "vector_store": {"schema_entities": "memory"},
            "fulltext_store": {
                "schema_entities": {
                    "target": "memory",
                    "params": {"tokenizer": "default"},
                }
            },
            "storage": {
                "schema_test": {
                    "target": "schema_composite",
                    "params": {
                        "kv_store": "default",
                        "vector_store": "default",
                        "fulltext_store": "default",
                        "graph_store": "default",
                    },
                }
            },
        }
    )
    context = default_context().merged(overlay)
    storage = StorageProducer.build_named("schema_test", context)
    assert isinstance(storage, CompositeStorage)
    return storage


def test_schema_composite_exposes_configured_entity_ports() -> None:
    storage = _schema_storage()

    assert storage.has_vector_port("schema_entities")
    assert storage.has_fulltext_port("schema_entities")
    assert storage.has_vector_port("layers_l0")
    assert storage.has_vector_port("layers_l1")


def test_schema_composite_does_not_create_undeclared_entity_ports() -> None:
    register_plugins()
    register_backends()
    register_schema_storage()
    overlay = AssemblyContext.from_dict(
        {
            "storage": {
                "schema_without_entities": {
                    "target": "schema_composite",
                    "params": {
                        "kv_store": "default",
                        "vector_store": "default",
                        "fulltext_store": "default",
                    },
                }
            }
        }
    )
    storage = StorageProducer.build_named(
        "schema_without_entities",
        default_context().merged(overlay),
    )

    assert not storage.has_vector_port("schema_entities")
    assert not storage.has_fulltext_port("schema_entities")


def test_schema_entity_ports_are_physically_independent_from_memory_ports() -> None:
    storage = _schema_storage()
    scope = Scope(org="schema-storage", user="alice")
    default_vector = storage.vector_port("default")
    entity_vector = storage.vector_port("schema_entities")
    default_fulltext = storage.fulltext_port("default")
    entity_fulltext = storage.fulltext_port("schema_entities")

    default_vector.insert(scope, [VectorRecord(id="same-id", vector=[1.0, 0.0])])
    entity_vector.insert(scope, [VectorRecord(id="same-id", vector=[0.0, 1.0])])
    default_fulltext.insert(scope, [Document(id="same-id", text="memory document")])
    entity_fulltext.insert(scope, [Document(id="same-id", text="entity document")])

    assert default_vector.get(scope, ["same-id"])[0].vector == [1.0, 0.0]
    assert entity_vector.get(scope, ["same-id"])[0].vector == [0.0, 1.0]
    assert default_fulltext.get(scope, ["same-id"])[0].text == "memory document"
    assert entity_fulltext.get(scope, ["same-id"])[0].text == "entity document"
