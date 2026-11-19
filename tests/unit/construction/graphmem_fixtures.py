"""Small real graphs and source trees for the optional GraphMem path."""

from copy import deepcopy
from dataclasses import replace

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor
from jiuwen_memory.common.type_def import (
    FeatureSet,
    HierarchyKind,
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    RetrievalPipeline,
    Scope,
    Segment,
)
from jiuwen_memory.common.type_def.memory import TreeFact, TreeMemoryData
from jiuwen_memory.construction.associator_impl.graphmem_topdown import TopDownGraphMemAssociator
from jiuwen_memory.construction.index_builder_impl.graph_relation_index_builder import (
    GraphRelationIndexBuilder,
)
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from tests.unit.construction.hierarchy_fixtures import make_leaf

HOME = Scope(org="org", space="research", user="alice", agent="assistant")


class Words(FeatureExtractor):
    def plugin_type(self):
        return PluginType.FEATURE_EXTRACTOR

    def health(self):
        return None

    def extract(self, text):
        return FeatureSet(keywords=text.lower().split())


class ReadRecordingDomain(CompositeDomainStore):
    def __init__(self, manager):
        super().__init__(manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK)
        self.reads = []

    def get(self, scope, unit_ids, **kwargs):
        self.reads.append((deepcopy(scope), list(unit_ids)))
        return super().get(scope, unit_ids, **kwargs)


def unit(uid, text="Alice bike riding", *, minute=0, scope=HOME, parent="", entities=None):
    result = make_leaf(uid, minute, scope=deepcopy(scope))
    result.segments = [Segment(content=text)]
    result.entities = entities if entities is not None else ["Alice"]
    result.hierarchy.parent_id = parent
    return result


def tree(parent_id, session, *, text="Alice bike travel", raw="Alice bought a bike in May."):
    scope = replace(HOME, session=session)
    child = unit("turn-1", raw, scope=scope, parent=parent_id)
    child.hierarchy.parent_scope = deepcopy(HOME)
    parent = unit(parent_id, text)
    parent.hierarchy.role = HierarchyRole.TIME_SPAN
    parent.hierarchy.child_ids = [child.id]
    parent.hierarchy.child_scopes = [deepcopy(scope)]
    parent.tree_memory = TreeMemoryData([TreeFact(child.id, deepcopy(scope), child.content)])
    return parent, child


def world(units: list[MemoryUnit]):
    graph = InMemoryGraphStore()
    manager = CompositeStoreManager(kv=InMemoryKVStore(), graph=graph)
    domain = ReadRecordingDomain(manager)
    manager.bind_domain_store(domain)
    for item in units:
        domain.add(item.scope, [item])
    associator = TopDownGraphMemAssociator(Words())
    writer = GraphRelationIndexBuilder(manager.graph())
    return domain, graph, associator, writer


def sync(associator, writer, units):
    previous = writer.fingerprints(units[0].scope, [item.id for item in units])
    delta = associator.associate_delta(units, previous)
    writer.apply_delta(units, delta)
    return delta


def multi_source_tree(uid, texts):
    parent, first = tree(uid, uid, text="Alice apples bananas")
    children = [unit(f"turn-{i}", text, scope=first.scope, parent=uid)
                for i, text in enumerate(texts)]
    for child in children:
        child.hierarchy.parent_scope = deepcopy(HOME)
    parent.hierarchy.child_ids = [child.id for child in children]
    parent.hierarchy.child_scopes = [deepcopy(first.scope) for _ in children]
    parent.tree_memory = TreeMemoryData([
        TreeFact(child.id, deepcopy(child.scope), child.content) for child in children
    ])
    return parent, children



def query(text="When did Alice buy the bike?"):
    return ParsedQuery(raw=text, hierarchy_kind=HierarchyKind.TIME,
                       hierarchy_role=HierarchyRole.TIME_SPAN)
