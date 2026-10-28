"""Rebuildable evidence graph projection from already committed relation MemoryUnits."""

import json
from dataclasses import asdict

from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, RelationStatus
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.types import Edge, IndexWriteMode, Node


class EvidenceGraphBuilder(IndexBuilder):
    def __init__(self, graph):
        if not graph.supports_incident_edges:
            raise ValidationError("evidence graph requires directed incident edge capability")
        self.graph = graph

    def operator_type(self):
        return OperatorType.INDEX_BUILDER

    def health(self):
        self.graph.health()

    def build(self, units, *, mode=IndexWriteMode.ALL):
        if mode != IndexWriteMode.RETRIEVAL_ONLY:
            raise UnsupportedCapabilityError("evidence_graph only projects committed records")
        for unit in units:
            relation = RelationRecord.parse(json.loads(unit.content))
            if relation.status == RelationStatus.REVOKED:
                self.graph.delete(
                    unit.scope, edge_ids=["continuity:relation:" + relation.relation_id]
                )
                continue
            for ref in (relation.source, relation.target):
                node = Node(ref.key, "memory-reference", {"ref": asdict(ref)})
                if self.graph.get(unit.scope, [ref.key]):
                    self.graph.update(unit.scope, nodes=[node])
                else:
                    self.graph.insert(unit.scope, nodes=[node])
            edge = Edge(
                "continuity:relation:" + relation.relation_id,
                relation.source.key,
                relation.target.key,
                relation.relation_type,
                {"ref": asdict(MemoryRef.of(unit))},
            )
            self.graph.delete(unit.scope, edge_ids=[edge.id])
            self.graph.insert(unit.scope, edges=[edge])

    def update(self, units, *, mode=IndexWriteMode.ALL):
        self.build(units, mode=mode)

    def remove(self, units, **kwargs):
        for unit in units:
            relation = RelationRecord.parse(json.loads(unit.content))
            self.graph.delete(unit.scope, edge_ids=["continuity:relation:" + relation.relation_id])

    def rebuild(self):
        raise UnsupportedCapabilityError("rebuild requires authorized relation head enumeration")


@IndexBuilderProducer.register("evidence_graph")
def build(config):
    manager = StoreManagerProducer.resolve(config)
    return EvidenceGraphBuilder(manager.graph(config.get("graph_store", "default")))
