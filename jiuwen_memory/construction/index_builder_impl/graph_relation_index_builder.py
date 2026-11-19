"""Scoped graph projection writes behind the construction IndexBuilder boundary."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.common.graph_memory import RELATION_KIND
from jiuwen_memory.common.type_def import MemoryUnit, Relation, Scope
from jiuwen_memory.construction.associator import AssociationDelta
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.types import Edge, IndexRemoveMode, IndexWriteMode, Node

_WINDOW_ID = "__graphmem_window_v1__"


class GraphRelationIndexBuilder(IndexBuilder):
    """Graph-only sub-builder. Fingerprints are committed after edge updates.

    Deterministic GraphMem edge ids make retries idempotent. Other producers' edges
    are preserved. This is recoverable ordering, not a multi-store transaction.
    """

    def __init__(self, graph: GraphStore, truth: DomainStore | None = None) -> None:
        self._graph = graph
        self._truth = truth
        self._lock = RLock()

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        self._graph.health()

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        for unit in units:
            try:
                self._graph.insert(unit.scope, nodes=[
                    Node(unit.id, unit.tier.value, {"content": unit.content}),
                ])
            except ConflictError:
                pass

    def update(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        for unit in units:
            found = self._graph.get(unit.scope, [unit.id])
            props = deepcopy(found[0].properties) if found else {}
            props["content"] = unit.content
            node = Node(unit.id, unit.tier.value, props)
            if found:
                self._graph.update(unit.scope, nodes=[node])
            else:
                self._graph.insert(unit.scope, nodes=[node])

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD,
    ) -> None:
        for unit in units:
            self._graph.delete(unit.scope, node_ids=[unit.id])

    def rebuild(self) -> None:
        raise ValidationError("graph relation rebuild requires an explicit evolve candidate window")

    def fingerprints(self, scope: Scope, unit_ids: list[str]) -> dict[str, str]:
        if _WINDOW_ID in unit_ids:
            raise ValidationError("reserved GraphMem window id")
        manifest = self._graph.get(scope, [_WINDOW_ID])
        if not manifest:
            return {}
        if manifest[0].label != "graphmem_control":
            raise ValidationError("GraphMem window id collides with an existing graph node")
        covered = set(manifest[0].properties.get("unit_ids", []))
        # Append to a covered window is incremental. A different/subset window
        # starts a new coverage context; nodes outside it are not marked checked.
        if not covered <= set(unit_ids):
            return {}
        return {node.id: node.properties.get("graphmem_fingerprint", "")
                for node in self._graph.get(scope, unit_ids) if node.id in covered}

    def write_legacy(self, units: list[MemoryUnit], relations: list[Relation]) -> None:
        if not units:
            return
        if any(unit.scope != units[0].scope for unit in units):
            raise ValidationError("relation writes require one exact Scope")
        self.build(units)
        if relations:
            self._graph.insert(units[0].scope, edges=[
                Edge(str(uuid4()), rel.source_id, rel.target_id, rel.relation, dict(rel.metadata))
                for rel in relations
            ])

    def apply_delta(self, units: list[MemoryUnit], delta: AssociationDelta) -> None:
        if not units or not delta.affected_ids:
            return
        scope = units[0].scope
        indexed = {unit.id: unit for unit in units}
        if any(unit.scope != scope for unit in units) or len(indexed) != len(units):
            raise ValidationError("relation delta requires unique ids in one exact Scope")
        affected = set(delta.affected_ids)
        if not affected <= indexed.keys():
            raise ValidationError("relation delta refers to units outside its candidate window")
        with self._lock:
            old = self._graph.get_edges(
                scope, delta.affected_ids, relation=RELATION_KIND, limit=10001,
            )
            if len(old) > 10000:
                raise ValidationError("relation maintenance edge limit exceeded; no writes made")
            # Partial windows must not silently leave invalid edges marked current.
            missing = sorted({uid for edge in old for uid in (edge.source, edge.target)
                              if uid not in indexed})
            if missing and (self._truth is None or len(missing) > 2000
                            or self._truth.get(scope, missing)):
                raise ValidationError("include existing relation neighbors in the evolve window")
            replacements = {}
            for rel in delta.relations:
                if (rel.relation != RELATION_KIND or rel.source_id not in indexed
                        or rel.target_id not in indexed or rel.source_id == rel.target_id):
                    raise ValidationError("invalid GraphMem relation endpoint")
                pair = sorted((rel.source_id, rel.target_id))
                edge_id = "gm:" + str(uuid5(NAMESPACE_URL, repr((scope, pair))))
                replacements[edge_id] = Edge(
                    edge_id, pair[0], pair[1], RELATION_KIND, deepcopy(rel.metadata),
                )
            # Updating the node content does not mark the relation plan current yet.
            self.update([indexed[uid] for uid in delta.affected_ids])
            existing = {edge.id for edge in old}
            for edge in replacements.values():
                if edge.id in existing:
                    self._graph.update(scope, edges=[edge])
                else:
                    self._graph.insert(scope, edges=[edge])
            obsolete = sorted(existing - replacements.keys())
            if obsolete:
                self._graph.delete(scope, edge_ids=obsolete)
            nodes = {node.id: node for node in self._graph.get(scope, delta.affected_ids)}
            for uid in delta.affected_ids:
                node = deepcopy(nodes[uid])
                node.properties["graphmem_fingerprint"] = (
                    delta.policy_id + ":" + delta.input_fingerprints.get(
                        uid, delta.fingerprints[uid])
                )
                self._graph.update(scope, nodes=[node])
            if missing:
                self._graph.delete(scope, node_ids=missing)
            manifest = Node(_WINDOW_ID, "graphmem_control", {"unit_ids": sorted(indexed)})
            if self._graph.get(scope, [_WINDOW_ID]):
                self._graph.update(scope, nodes=[manifest])
            else:
                self._graph.insert(scope, nodes=[manifest])
