"""Persistent derived graph using a named KV backend; no memory truth is stored here."""
from __future__ import annotations

import json
from dataclasses import asdict
from threading import RLock

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import canonical, fingerprint
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node


class KVGraphStore(GraphStore):
    """Single-writer projection; edge truth precedes repairable adjacency indexes."""

    def __init__(self, kv: KVStore):
        self._kv = kv
        self._lock = RLock()

    @property
    def supports_incident_edges(self) -> bool:
        return True

    @property
    def supports_conditional_projection(self) -> bool:
        return True

    def _adj(self, scope, node_id):
        try:
            return self._read(scope, "adj", node_id)
        except NotFoundError:
            return []

    def _set_adj(self, scope, node_id, ids):
        self._put(scope, "adj", node_id, ids)

    def _read_tomb(self, scope, edge_id):
        try:
            return int(self._read(scope, "tomb", edge_id).get("revision") or 0)
        except NotFoundError:
            return 0

    def _set_tomb(self, scope, edge_id, revision):
        self._put(scope, "tomb", edge_id, {"revision": revision})

    def project_edge(self, scope, edge, *, revision, nodes=None):
        with self._lock:
            for node in nodes or []:
                self._put(scope, "node", node.id, asdict(node))
            props = {**(edge.properties or {}), "revision": revision}
            projected = Edge(edge.id, edge.source, edge.target, edge.relation, props)
            try:
                current = self._read(scope, "edge", edge.id)
            except NotFoundError:
                current = None
            tomb = self._read_tomb(scope, edge.id)
            if current is None:
                if tomb >= revision:
                    # The edge was revoked at a revision not older than this one: an
                    # old projection must not resurrect what the truth already revoked.
                    raise ConflictError("edge", edge.id)
                self._put(scope, "edge", edge.id, asdict(projected))
                for endpoint in (edge.source, edge.target):
                    ids = self._adj(scope, endpoint)
                    if edge.id not in ids:
                        ids.append(edge.id)
                        self._set_adj(scope, endpoint, ids)
                return
            existing = int(current.get("properties", {}).get("revision") or 0)
            if existing == revision:
                # Same revision must carry the same content; anything else is a conflict
                # that a blind replay must not mask by silently keeping the old edge.
                if current == asdict(projected):
                    return
                raise ConflictError("edge", edge.id)
            if existing > revision:
                raise ConflictError("edge", edge.id)
            self._put(scope, "edge", edge.id, asdict(projected))
            previous = {current["source"], current["target"]}
            for endpoint in previous - {edge.source, edge.target}:
                ids = self._adj(scope, endpoint)
                if edge.id in ids:
                    self._set_adj(scope, endpoint, [i for i in ids if i != edge.id])
            for endpoint in {edge.source, edge.target} - previous:
                ids = self._adj(scope, endpoint)
                if edge.id not in ids:
                    ids.append(edge.id)
                    self._set_adj(scope, endpoint, ids)

    def unproject_edge(self, scope, edge_id, *, revision):
        with self._lock:
            try:
                current = self._read(scope, "edge", edge_id)
            except NotFoundError:
                current = None
            tomb = max(self._read_tomb(scope, edge_id), revision)
            self._set_tomb(scope, edge_id, tomb)
            if current is None:
                return  # Idempotent revoke; tombstone already blocks old replays.
            if int(current.get("properties", {}).get("revision") or 0) > revision:
                return  # A newer revision is projected; do not erase it.
            self._kv.delete(scope, self._key("edge", edge_id))
            for endpoint in (current["source"], current["target"]):
                ids = self._adj(scope, endpoint)
                if edge_id in ids:
                    self._set_adj(scope, endpoint, [i for i in ids if i != edge_id])

    def resource_descriptor(self) -> str | None:
        descriptor = self._kv.resource_descriptor()
        return None if descriptor is None else f"kv-graph://{descriptor}"

    def store_type(self):
        return StoreType.GRAPH

    def health(self):
        self._kv.health()

    def close(self):
        """The injected KV is borrowed; its assembly root closes it once."""
        return None

    @staticmethod
    def _key(kind, identity):
        return f"/graph/{kind}/{fingerprint(identity)}"

    def _read(self, scope, kind, identity):
        return json.loads(self._kv.get(scope, self._key(kind, identity)))

    def _put(self, scope, kind, identity, value):
        key = self._key(kind, identity)
        raw = canonical(value).encode()
        if self._kv.exists(scope, key):
            self._kv.update(scope, key, raw)
        else:
            self._kv.insert(scope, key, raw)

    def _write(self, scope, nodes, edges, update):
        with self._lock:
            for kind, records in (("node", nodes or []), ("edge", edges or [])):
                for record in records:
                    exists = self._kv.exists(scope, self._key(kind, record.id))
                    if exists != update:
                        error = NotFoundError if update else ConflictError
                        raise error(kind, record.id)
                    self._put(scope, kind, record.id, asdict(record))
                    if kind == "edge":
                        for endpoint in (record.source, record.target):
                            try:
                                ids = self._read(scope, "adj", endpoint)
                            except NotFoundError:
                                ids = []
                            if record.id not in ids:
                                ids.append(record.id)
                            self._put(scope, "adj", endpoint, ids)

    def insert(self, scope: Scope, nodes=None, edges=None):
        self._write(scope, nodes, edges, False)

    def update(self, scope: Scope, nodes=None, edges=None):
        self._write(scope, nodes, edges, True)

    def delete(self, scope: Scope, node_ids=None, edge_ids=None):
        with self._lock:
            for nid in node_ids or []:
                try:
                    ids = self._read(scope, "adj", nid)
                except NotFoundError:
                    ids = []
                for eid in ids:
                    try:
                        edge = self._read(scope, "edge", eid)
                    except NotFoundError:
                        continue
                    if nid in (edge["source"], edge["target"]):
                        self._kv.delete(scope, self._key("edge", eid))
                self._kv.delete(scope, self._key("adj", nid))
                self._kv.delete(scope, self._key("node", nid))
            for eid in edge_ids or []:
                self._kv.delete(scope, self._key("edge", eid))

    def get(self, scope: Scope, node_ids):
        nodes = []
        for nid in node_ids:
            try:
                nodes.append(Node(**self._read(scope, "node", nid)))
            except NotFoundError:
                continue
        return nodes

    def incident_edges(self, scope: Scope, node_id: str, *, limit: int = 100):
        with self._lock:
            try:
                ids = self._read(scope, "adj", node_id)
            except NotFoundError:
                return []
            edges = []
            for eid in ids:
                try:
                    edge = Edge(**self._read(scope, "edge", eid))
                except NotFoundError:
                    continue
                if node_id in (edge.source, edge.target):
                    edges.append(edge)
                    if len(edges) >= limit:
                        break
            return edges

    def seed_ids(self, scope: Scope, tokens: set[str], *, extensions=None):
        if not tokens:
            return []
        matches = []
        for _, raw in self._kv.scan(scope, "/graph/node/"):
            node = json.loads(raw)
            content = str(node["properties"].get("content", ""))
            if any(token in content for token in tokens):
                matches.append(node["id"])
        return matches

    def search(self, scope: Scope, query: GraphQuery):
        visited = {query.start_id}
        frontier = [query.start_id]
        reached = []
        for _ in range(query.depth):
            next_frontier = []
            for nid in frontier:
                for edge in self.incident_edges(scope, nid, limit=query.limit):
                    if query.relation and edge.relation != query.relation:
                        continue
                    other = edge.target if edge.source == nid else edge.source
                    if other not in visited:
                        visited.add(other)
                        reached.append(other)
                        next_frontier.append(other)
                        if len(reached) >= query.limit:
                            return self.get(scope, reached)
            frontier = next_frontier
        return self.get(scope, reached)


@GraphProducer.register("kv")
def build(config):
    return KVGraphStore(KvProducer.build_named(config.get("kv_store", "default"), config.ctx))
