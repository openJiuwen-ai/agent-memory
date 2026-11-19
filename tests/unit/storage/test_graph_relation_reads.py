"""The edge projection read preserves storage authorization and routing contracts."""

import pytest

from jiuwen_memory.common.type_def import RetrievalPipeline, Scope
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.security import StorageAction, StorageSecurity
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import Edge, Node

pytestmark = pytest.mark.unit


class RecordingSecurity(StorageSecurity):
    def __init__(self):
        self.calls = []

    def authorize(self, access, scope, action, resource):
        self.calls.append((scope, action, resource))


def test_domain_edge_read_authorizes_search_at_both_boundaries_and_named_port():
    scope = Scope(org="org", user="alice")
    graph, other = InMemoryGraphStore(), InMemoryGraphStore()
    graph.insert(scope, nodes=[Node("a"), Node("b")], edges=[Edge("e", "a", "b", "graphmem")])
    security = RecordingSecurity()
    manager = CompositeStoreManager(graph={"selected": graph, "default": other}, security=security)
    domain = CompositeDomainStore(manager=manager,
                                  preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK,
                                  graph_name="selected")
    result = domain.related_edges(scope, ["a"], relation="graphmem")
    assert [edge.id for edge in result] == ["e"]
    assert security.calls == [(scope, StorageAction.SEARCH, "memory_unit"),
                              (scope, StorageAction.SEARCH, "graph")]
