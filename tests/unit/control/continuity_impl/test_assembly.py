"""Assembly validation: one graph object for query and projection; builder capability."""

import pytest

from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.construction.index_builder import IndexBuilder, require_full_write_chain
from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from jiuwen_memory.control.continuity_impl.service import ContinuityService
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.types import IndexWriteMode

pytestmark = pytest.mark.unit


class _FullBuilder(IndexBuilder):
    def operator_type(self):
        from jiuwen_memory.construction.base import OperatorType

        return OperatorType.INDEX_BUILDER

    def health(self):
        return None

    def build(self, units, *, mode=IndexWriteMode.ALL):
        pass

    def update(self, units, *, mode=IndexWriteMode.ALL):
        pass

    def remove(self, units, **kwargs):
        pass

    def rebuild(self):
        pass


def test_evidence_graph_declares_only_retrieval_write_mode():
    builder = EvidenceGraphBuilder(InMemoryGraphStore())
    assert builder.supported_write_modes() == {IndexWriteMode.RETRIEVAL_ONLY}


def test_retrieval_only_builder_is_rejected_from_main_write_chain():
    with pytest.raises(ValidationError, match="ALL write mode"):
        require_full_write_chain(EvidenceGraphBuilder(InMemoryGraphStore()))


def test_index_only_builders_are_rejected_from_main_write_chain():
    from unittest.mock import Mock

    from jiuwen_memory.construction.index_builder_impl.fulltext_index_builder import (
        FulltextIndexBuilder,
    )

    storage = Mock()
    storage.has_fulltext.return_value = False
    builder = FulltextIndexBuilder(storage, layers_enabled=True)
    assert builder.supported_write_modes() == {IndexWriteMode.RETRIEVAL_ONLY}
    with pytest.raises(ValidationError, match="ALL write mode"):
        require_full_write_chain(builder)


def test_full_builder_is_accepted_for_main_write_chain():
    assert require_full_write_chain(_FullBuilder()) is not None


def test_mismatched_projector_graph_is_rejected(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        repository = ContinuityRepository(kv, single_writer=True, allow_ephemeral=True)
        view_graph = InMemoryGraphStore()
        projector_graph = InMemoryGraphStore()  # different object, same port name
        with pytest.raises(ValidationError, match="share one GraphStore"):
            ContinuityService(
                repository,
                view_graph,
                RoleViewBuilder(),
                projector=EvidenceGraphBuilder(projector_graph),
            )
    finally:
        kv.close()


def test_matching_projector_graph_is_accepted(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        repository = ContinuityRepository(kv, single_writer=True, allow_ephemeral=True)
        graph = InMemoryGraphStore()
        service = ContinuityService(
            repository, graph, RoleViewBuilder(), projector=EvidenceGraphBuilder(graph)
        )
        service.health()
    finally:
        kv.close()


def test_continuity_mode_requires_no_graph_and_gates_relations(tmp_path):
    from jiuwen_memory.common.type_def import Scope
    from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord

    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        repository = ContinuityRepository(kv, single_writer=True, allow_ephemeral=True)
        service = ContinuityService(repository, mode="continuity")
        assert service.health() == {"continuity_mode": "continuity"}
        scope = Scope(org="acme", space="project")
        a = MemoryRef(scope, "a", "0" * 64)
        b = MemoryRef(scope, "b", "1" * 64)
        with pytest.raises(UnsupportedCapabilityError):
            service.relation(
                scope, RelationRecord("r", a, b, "supports"),
                0, lambda ref: None, lambda text, metadata=None: [],
            )
    finally:
        kv.close()


def test_role_mode_without_graph_or_view_is_rejected(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        repository = ContinuityRepository(kv, single_writer=True, allow_ephemeral=True)
        with pytest.raises(ValidationError, match="role mode requires"):
            ContinuityService(repository, mode="role")
    finally:
        kv.close()


def test_invalid_mode_is_rejected(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        repository = ContinuityRepository(kv, single_writer=True, allow_ephemeral=True)
        with pytest.raises(ValidationError, match="continuity mode"):
            ContinuityService(repository, mode="neither")
    finally:
        kv.close()


def test_agent_engine_assembly_rejects_retrieval_only_builder(monkeypatch):
    from unittest.mock import Mock

    from jiuwen_memory.control.engine_impl import agent_engine

    monkeypatch.setattr(agent_engine, "resolve_index_builder_default", lambda _: "evidence_graph")
    monkeypatch.setattr(agent_engine.StoreManagerProducer, "resolve", lambda _: Mock())
    monkeypatch.setattr(agent_engine.IngestorProducer, "dep", lambda *a, **kw: Mock())
    monkeypatch.setattr(agent_engine.IndexBuilderProducer, "dep", lambda *a, **kw:
                        EvidenceGraphBuilder(InMemoryGraphStore()))
    with pytest.raises(ValidationError, match="ALL write mode"):
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        agent_engine._build(Mock())
