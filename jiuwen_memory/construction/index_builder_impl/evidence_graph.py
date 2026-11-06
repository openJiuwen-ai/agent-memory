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
        if not graph.supports_conditional_projection:
            raise ValidationError("evidence graph requires conditional projection capability")
        self.graph = graph

    def projection_graph(self):
        return self.graph

    def operator_type(self):
        return OperatorType.INDEX_BUILDER

    def supported_write_modes(self):
        return {IndexWriteMode.RETRIEVAL_ONLY}

    def health(self):
        self.graph.health()

    def build(self, units, *, mode=IndexWriteMode.ALL):
        if mode != IndexWriteMode.RETRIEVAL_ONLY:
            raise UnsupportedCapabilityError("evidence_graph only projects committed records")
        for unit in units:
            relation = RelationRecord.parse(json.loads(unit.content))
            edge_id = "continuity:relation:" + relation.relation_id
            if relation.status == RelationStatus.REVOKED:
                # Revoke is itself a revision; conditionally remove the prior projection
                # without ever deleting a newer revision or any unrelated edge.
                self.graph.unproject_edge(unit.scope, edge_id, revision=relation.revision)
                continue
            nodes = [
                Node(ref.key, "memory-reference", {"ref": asdict(ref)})
                for ref in (relation.source, relation.target)
            ]
            edge = Edge(
                edge_id,
                relation.source.key,
                relation.target.key,
                relation.relation_type,
                {"ref": asdict(MemoryRef.of(unit))},
            )
            # Conditional projection is idempotent by revision: a replay neither
            # duplicates the edge nor lets an old revision overwrite a newer one.
            self.graph.project_edge(
                unit.scope, edge, revision=relation.revision, nodes=nodes
            )

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
