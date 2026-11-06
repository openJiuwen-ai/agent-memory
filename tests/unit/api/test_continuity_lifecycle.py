"""Runtime lifecycle: public close releases continuity single-writer and reassembles."""

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.config import Config

pytestmark = pytest.mark.unit


def _continuity_config(tmp_path) -> Config:
    return Config.from_dict(
        {
            "engine": {
                "default": {
                    "target": "cloud",
                    "params": dict.fromkeys(
                        ("ingestor", "retriever", "evolver", "index_builder",
                         "classifier", "scheduler", "policy"),
                        "default",
                    ),
                }
            },
            "authorizer": {
                "default": {"target": "space_aware", "params": {"delegate": "standard"}},
                "standard": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                },
            },
            "kv_store": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "mem.db"), "fixed_path": True},
                },
                "registry": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "registry.db"), "fixed_path": True},
                },
                "evidence": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "graph.db"), "fixed_path": True},
                },
            },
            "graph_store": {"evidence": {"target": "kv", "params": {"kv_store": "evidence"}}},
            "constructor": {
                "evidence": {"target": "evidence_graph", "params": {"graph_store": "evidence"}}
            },
            "role_view": {"default": {"target": "bounded"}},
            "continuity": {
                "default": {
                    "target": "local",
                    "params": {
                        "single_writer": True,
                        "mode": "role",
                        "registry_store": "registry",
                        "graph_store": "evidence",
                        "relation_index_builder": "evidence",
                        "role_view": "default",
                    },
                }
            },
        }
    )


def test_runtime_close_releases_single_writer_and_reassembles(tmp_path):
    config = _continuity_config(tmp_path)
    first = assemble_runtime(config=config)
    first.close(wait=True)
    # A second assembly over the same registry file must succeed only because the
    # first runtime released the single-writer claim on close.
    second = assemble_runtime(config=config)
    second.close(wait=True)


def test_repeated_close_and_reassemble_cycles(tmp_path):
    config = _continuity_config(tmp_path)
    # Single-writer means one live write service at a time; each close must release
    # the claim so the next assembly can take it.
    for _ in range(4):
        runtime = assemble_runtime(config=config)
        runtime.close(wait=True)


def test_service_close_releases_lock_and_is_idempotent(tmp_path):
    from jiuwen_memory.construction.index_builder_impl.evidence_graph import (
        EvidenceGraphBuilder,
    )
    from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
    from jiuwen_memory.control.continuity_impl.service import ContinuityService
    from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
    from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

    path = str(tmp_path / "registry.db")
    kv = SQLiteKVStore(path)
    graph = InMemoryGraphStore()
    service = ContinuityService(
        ContinuityRepository(kv, single_writer=True),
        graph,
        RoleViewBuilder(),
        projector=EvidenceGraphBuilder(graph),
    )
    # Closing twice is safe; the registry KV close releases the single-writer claim.
    service.close()
    service.close()
    fresh = SQLiteKVStore(path)
    try:
        ContinuityRepository(fresh, single_writer=True)  # must not raise
    finally:
        fresh.close()


def test_failed_service_assembly_releases_claim_for_retry(tmp_path):
    from jiuwen_memory.common.errors import AgentMemoryError

    config = _continuity_config(tmp_path)
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    config._data["continuity"]["default"]["params"]["role_view"] = "missing-view"
    with pytest.raises(AgentMemoryError):
        assemble_runtime(config=config)
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    config._data["continuity"]["default"]["params"]["role_view"] = "default"
    runtime = assemble_runtime(config=config)
    runtime.close()


def test_service_close_releases_claim_without_closing_borrowed_store(tmp_path):
    from unittest.mock import Mock

    from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
    from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
    from jiuwen_memory.control.continuity_impl.service import ContinuityService
    from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
    from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

    kv = SQLiteKVStore(str(tmp_path / "borrowed.db"))
    original_close = kv.close
    kv.close = Mock(wraps=original_close)
    graph = InMemoryGraphStore()
    service = ContinuityService(ContinuityRepository(kv, single_writer=True), graph,
                                RoleViewBuilder(), projector=EvidenceGraphBuilder(graph))
    try:
        service.close()
        service.close()
        kv.close.assert_not_called()
        kv.health()
        repository = ContinuityRepository(kv, single_writer=True)
        repository.close()
    finally:
        original_close()
