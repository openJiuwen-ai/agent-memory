"""Neo4j GraphStore — persistent property graph with transactional writes.

The Neo4j driver is an optional dependency: the store registers without importing it,
and raises a clear ``BackendError`` if the driver is missing or the cluster is
unreachable when the backend is actually assembled or first used. ``uri``,
``database``, ``username`` and ``password`` follow the storage late-binding
convention (``graph_store.<field>`` via ConfigSource).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.shard_search import graph_node_allowed, graph_shard_filter
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

from .._support import read_ssl_config, require_tls_scheme, wrap_backend


class Neo4jGraphStore(GraphStore):
    """Attribute-graph backend; one transaction per write, Scope enforced by ``scope_key``."""

    def __init__(
        self,
        uri: str,
        *,
        database: str = "neo4j",
        username: str | None = None,
        password: str | None = None,
        config_source=None,
        config_namespace: str = "graph_store",
        driver=None,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
    ) -> None:
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = ssl_ca_cert
        self._validate_tls(uri)
        self._fallback_uri = uri
        self._fallback_database = database
        self._fallback_username = username
        self._fallback_password = password
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._driver_obj = driver
        self._injected = driver is not None
        self._driver_fingerprint: object | None = None
        self._schema_ready = False

    # -- properties --------------------------------------------------------

    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset({StoreCapability.SHARD_PREFILTER})

    def resource_descriptor(self) -> str | None:
        from .._support import sanitize_resource_id

        uri = sanitize_resource_id(str(self._resolved("uri", self._fallback_uri)))
        database = str(self._resolved("database", self._fallback_database))
        return f"neo4j://{uri}/{database}"

    @property
    def supports_incident_edges(self) -> bool:
        return True

    @property
    def supports_conditional_projection(self) -> bool:
        return True

    def store_type(self) -> StoreType:
        return StoreType.GRAPH

    # -- scope + codec -----------------------------------------------------

    @staticmethod
    def _scope_key(scope: Scope) -> str:
        return json.dumps(asdict(scope), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _node_params(node: Node) -> dict[str, Any]:
        content = str(node.properties.get("content", ""))
        props = {k: v for k, v in node.properties.items() if k != "content"}
        return {
            "id": node.id,
            "label": node.label,
            "content": content,
            "props": json.dumps(props, ensure_ascii=False),
            "shard": node.properties.get("storage_shard"),
            "shard_next": node.properties.get("storage_shard_next"),
        }

    @staticmethod
    def _node_from_record(record: dict[str, Any]) -> Node:
        props = json.loads(record.get("props") or "{}")
        props["content"] = record.get("content", "")
        return Node(record["id"], record.get("label", ""), props)

    @staticmethod
    def _edge_params(edge: Edge) -> dict[str, Any]:
        return {
            "id": edge.id,
            "relation": edge.relation,
            "props": json.dumps(edge.properties or {}, ensure_ascii=False),
        }

    @staticmethod
    def _edge_from_record(record: dict[str, Any]) -> Edge:
        props = json.loads(record.get("props") or "{}")
        return Edge(
            record["id"], record["source"], record["target"], record.get("relation", ""), props
        )

    # -- driver + session ---------------------------------------------------

    def _resolved(self, field: str, fallback: Any) -> Any:
        from jiuwen_memory.config.active import resolve_bound_value

        value = resolve_bound_value(
            self._config_source, namespace=self._config_namespace, field=field, fallback=fallback
        )
        return value if value not in (None, "") else fallback

    def _validate_tls(self, uri):
        if self._ssl_verify:
            require_tls_scheme(
                uri, expected="neo4j+s" if str(uri).startswith("neo4j") else "bolt+s",
                component="neo4j", param="params.uri",
            )

    def _connect_params(self) -> dict[str, Any]:
        return {
            "uri": self._resolved("uri", self._fallback_uri),
            "database": self._resolved("database", self._fallback_database),
            "username": self._resolved("username", self._fallback_username),
            "password": self._resolved("password", self._fallback_password),
        }

    @property
    def _driver(self):
        if self._injected:
            return self._driver_obj
        params = self._connect_params()
        self._validate_tls(params["uri"])
        fingerprint = tuple(params[k] for k in ("uri", "database", "username", "password"))
        if self._driver_obj is not None and self._driver_fingerprint == fingerprint:
            return self._driver_obj
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise BackendError(
                "neo4j driver not installed (install the 'deploy' extra: pip install neo4j)"
            ) from exc
        auth = (params["username"], params["password"]) if params["username"] else None
        with wrap_backend("neo4j connect"):
            uri = params["uri"]
            options = {}
            if self._ssl_verify:
                from neo4j import TrustCustomCAs

                # The driver forbids custom trust with +s. Keep TLS mandatory while
                # translating the validated scheme into explicit encryption options.
                uri = uri.replace("+s://", "://", 1)
                options = {
                    "encrypted": True,
                    "trusted_certificates": TrustCustomCAs(self._ssl_ca_cert),
                }
            replacement = GraphDatabase.driver(uri, auth=auth, **options)
            previous = self._driver_obj
            self._driver_obj = replacement
            self._schema_ready = False
            if previous is not None:
                previous.close()
        self._driver_fingerprint = fingerprint
        return self._driver_obj

    def _session(self):
        return self._driver.session(database=self._resolved("database", self._fallback_database))

    @staticmethod
    def _rows(result) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for record in result:
            data = getattr(record, "data", None)
            out.append(data() if callable(data) else dict(record))
        return out

    def _run(self, query: str, **params) -> list[dict[str, Any]]:
        with wrap_backend("neo4j query"):
            with self._session() as session:
                return self._rows(session.run(query, **params))

    @contextmanager
    def _write(self, scope):
        # One transaction owns the scope lock, checks and mutation. The constraint on
        # MemGraphLock makes first-writer MERGE serialize across processes as well.
        driver = self._driver  # Resolve configuration before checking the schema cache.
        if not self._schema_ready:
            self._ensure_schema()
        with wrap_backend("neo4j write"):
            database = self._resolved("database", self._fallback_database)
            with driver.session(database=database) as session:
                with session.begin_transaction() as tx:
                    self._rows(
                        tx.run(
                            "MERGE (g:MemGraphLock {scope_key:$sk}) "
                            "SET g.serial=coalesce(g.serial,0)+1",
                            sk=self._scope_key(scope),
                        )
                    )
                    yield tx
                    tx.commit()

    def _contains(self, tx, scope, kind, identity):
        if kind == "node":
            query = "MATCH (n:MemNode {scope_key:$sk, id:$id}) RETURN n.id AS id"
        else:
            query = "MATCH ()-[e:MEM_EDGE {scope_key:$sk, id:$id}]->() RETURN e.id AS id"
        return bool(self._rows(tx.run(query, sk=self._scope_key(scope), id=identity)))

    def _put_edge(self, tx, sk, edge):
        rows = self._rows(
            tx.run(
                "MATCH (s:MemNode {scope_key:$sk, id:$source}), "
                "(t:MemNode {scope_key:$sk, id:$target}) "
                "CREATE (s)-[e:MEM_EDGE {scope_key:$sk, id:$id, relation:$relation, "
                "props:$props}]->(t) RETURN e.id AS id",
                sk=sk,
                source=edge.source,
                target=edge.target,
                **self._edge_params(edge),
            )
        )
        if not rows:
            raise NotFoundError("edge endpoint", edge.id)

    def insert(self, scope: Scope, nodes=None, edges=None):
        sk = self._scope_key(scope)
        with self._write(scope) as tx:
            for node in nodes or []:
                if self._contains(tx, scope, "node", node.id):
                    raise ConflictError("node", node.id)
                self._rows(
                    tx.run(
                        "CREATE (n:MemNode {scope_key:$sk, id:$id, label:$label, "
                        "content:$content, props:$props, storage_shard:$shard, "
                        "storage_shard_next:$shard_next})",
                        sk=sk,
                        **self._node_params(node),
                    )
                )
            for edge in edges or []:
                if self._contains(tx, scope, "edge", edge.id):
                    raise ConflictError("edge", edge.id)
                self._put_edge(tx, sk, edge)

    def update(self, scope: Scope, nodes=None, edges=None):
        sk = self._scope_key(scope)
        with self._write(scope) as tx:
            for node in nodes or []:
                if not self._contains(tx, scope, "node", node.id):
                    raise NotFoundError("node", node.id)
                self._rows(
                    tx.run(
                        "MATCH (n:MemNode {scope_key:$sk, id:$id}) "
                        "SET n.label=$label, n.content=$content, n.props=$props, "
                        "n.storage_shard=$shard, n.storage_shard_next=$shard_next",
                        sk=sk,
                        **self._node_params(node),
                    )
                )
            for edge in edges or []:
                if not self._contains(tx, scope, "edge", edge.id):
                    raise NotFoundError("edge", edge.id)
                self._rows(
                    tx.run(
                        "MATCH ()-[e:MEM_EDGE {scope_key:$sk, id:$id}]->() DELETE e",
                        sk=sk,
                        id=edge.id,
                    )
                )
                self._put_edge(tx, sk, edge)

    def delete(self, scope: Scope, node_ids=None, edge_ids=None):
        sk = self._scope_key(scope)
        with self._write(scope) as tx:
            for node_id in node_ids or []:
                self._rows(
                    tx.run(
                        "MATCH (n:MemNode {scope_key:$sk, id:$id}) DETACH DELETE n",
                        sk=sk,
                        id=node_id,
                    )
                )
            for edge_id in edge_ids or []:
                self._rows(
                    tx.run(
                        "MATCH ()-[e:MEM_EDGE {scope_key:$sk, id:$id}]->() DELETE e",
                        sk=sk,
                        id=edge_id,
                    )
                )

    def get(self, scope: Scope, node_ids):
        if not node_ids:
            return []
        sk = self._scope_key(scope)
        rows = self._run(
            "MATCH (n:MemNode {scope_key:$sk}) WHERE n.id IN $ids "
            "RETURN n.id AS id, n.label AS label, n.content AS content, n.props AS props",
            sk=sk,
            ids=list(node_ids),
        )
        return [self._node_from_record(r) for r in rows]

    def incident_edges(self, scope: Scope, node_id: str, *, limit: int = 100):
        sk = self._scope_key(scope)
        rows = self._run(
            "MATCH (n:MemNode {scope_key:$sk, id:$id})"
            "-[e:MEM_EDGE {scope_key:$sk}]-(m:MemNode {scope_key:$sk}) "
            "RETURN e.id AS id, e.relation AS relation, e.props AS props, "
            "startNode(e).id AS source, endNode(e).id AS target "
            "ORDER BY e.id LIMIT $limit",
            sk=sk,
            id=node_id,
            limit=limit,
        )
        return [self._edge_from_record(r) for r in rows]

    def seed_ids(
        self,
        scope: Scope,
        tokens: set[str],
        *,
        extensions: dict | None = None,
    ):
        if not tokens:
            return []
        selected = graph_shard_filter(extensions)
        sk = self._scope_key(scope)
        rows = self._run(
            "MATCH (n:MemNode {scope_key:$sk}) "
            "WHERE any(t IN $tokens WHERE n.content CONTAINS t) "
            "AND ($shards IS NULL OR n.storage_shard IN $shards "
            "OR n.storage_shard_next IN $shards "
            "OR (n.storage_shard IS NULL AND n.storage_shard_next IS NULL)) "
            "RETURN n.id AS id",
            sk=sk,
            tokens=list(tokens),
            shards=sorted(selected) if selected is not None else None,
        )
        return [r["id"] for r in rows]

    def search(self, scope: Scope, query: GraphQuery):
        # Parameterized variable-length bounds are not portable across supported Neo4j
        # versions. Bounded BFS also checks every intermediate node's real Scope.
        if query.depth <= 0 or query.limit <= 0:
            return []
        selected = graph_shard_filter(query.extensions)
        if selected is not None:
            start = self.get(scope, [query.start_id])
            if not start or not graph_node_allowed(start[0].properties, selected):
                return []
        visited = {query.start_id}
        frontier = [query.start_id]
        reached = []
        for _ in range(query.depth):
            following = []
            for node in frontier:
                rows = self._run(
                    "MATCH (n:MemNode {scope_key:$sk, id:$id})"
                    "-[e:MEM_EDGE {scope_key:$sk}]-(m:MemNode {scope_key:$sk}) "
                    "WHERE ($relation IS NULL OR e.relation=$relation) "
                    "RETURN DISTINCT m.id AS id, m.storage_shard AS shard, "
                    "m.storage_shard_next AS shard_next ORDER BY id LIMIT $cap",
                    sk=self._scope_key(scope),
                    id=node,
                    relation=query.relation,
                    # 枢纽节点可能有大量邻居；保守上限避免单跳读爆，但不改变
                    # 正常分片数据的遍历语义。超限属于已知遗留。
                    cap=max(64, (query.limit + len(visited)) * 8),
                )
                for row in rows:
                    if row["id"] in visited:
                        continue
                    # 先经过节点，再决定是否返回；分片条件不能放进邻居匹配，
                    # 否则无法经由其它分片的节点继续多跳。
                    visited.add(row["id"])
                    following.append(row["id"])
                    props = {
                        "storage_shard": row.get("shard"),
                        "storage_shard_next": row.get("shard_next"),
                    }
                    if not graph_node_allowed(props, selected):
                        continue
                    reached.append(row["id"])
                    if len(reached) >= query.limit:
                        return self.get(scope, reached)
            frontier = following
            if not frontier:
                break
        return self.get(scope, reached)

    def _projection(self, scope, edge_id, revision, edge, nodes):
        if type(revision) is not int or revision < 1:
            raise ValueError("positive projection revision required")
        sk = self._scope_key(scope)
        payload = json.dumps(asdict(edge) if edge else None, sort_keys=True)
        with self._write(scope) as tx:
            rows = self._rows(
                tx.run(
                    "MATCH (p:MemProjection {scope_key:$sk, id:$id}) "
                    "RETURN p.revision AS revision, p.payload AS payload",
                    sk=sk,
                    id=edge_id,
                )
            )
            if rows:
                current = rows[0]
                if current["revision"] > revision:
                    raise ConflictError("projection", edge_id)
                if current["revision"] == revision:
                    if current["payload"] != payload:
                        raise ConflictError("projection", edge_id)
                    return
            for node in nodes or []:
                self._rows(
                    tx.run(
                        "MERGE (n:MemNode {scope_key:$sk, id:$id}) "
                        "SET n.label=$label, n.content=$content, n.props=$props",
                        sk=sk,
                        **self._node_params(node),
                    )
                )
            self._rows(
                tx.run(
                    "MATCH ()-[e:MEM_EDGE {scope_key:$sk, id:$id}]->() DELETE e",
                    sk=sk,
                    id=edge_id,
                )
            )
            if edge is not None:
                self._put_edge(tx, sk, edge)
            # Durable tombstone survives deletion of the visible edge.
            self._rows(
                tx.run(
                    "MERGE (p:MemProjection {scope_key:$sk, id:$id}) "
                    "SET p.revision=$revision, p.payload=$payload",
                    sk=sk,
                    id=edge_id,
                    revision=revision,
                    payload=payload,
                )
            )

    def project_edge(self, scope: Scope, edge: Edge, *, revision: int, nodes=None):
        self._projection(scope, edge.id, revision, edge, nodes)

    def unproject_edge(self, scope: Scope, edge_id: str, *, revision: int):
        self._projection(scope, edge_id, revision, None, None)

    # -- lifecycle ----------------------------------------------------------

    def _ensure_schema(self):
        for statement in (
            "CREATE CONSTRAINT mem_graph_lock IF NOT EXISTS "
            "FOR (n:MemGraphLock) REQUIRE n.scope_key IS UNIQUE",
            "CREATE CONSTRAINT mem_node_identity IF NOT EXISTS "
            "FOR (n:MemNode) REQUIRE (n.scope_key,n.id) IS UNIQUE",
            "CREATE CONSTRAINT mem_projection_identity IF NOT EXISTS "
            "FOR (n:MemProjection) REQUIRE (n.scope_key,n.id) IS UNIQUE",
        ):
            self._run(statement)
        self._schema_ready = True

    def health(self):
        self._run("RETURN 1")

    def close(self):
        if self._driver_obj is not None:
            self._driver_obj.close()
            self._driver_obj = None


@GraphProducer.register("neo4j")
def build(config):
    uri = Factory.require_param(config, "uri", backend="neo4j")
    database = Factory.cfg_get(config, "database", "neo4j")
    username = Factory.require_param(config, "username", backend="neo4j")
    password = Factory.require_param(config, "password", backend="neo4j")
    ssl = read_ssl_config(config, backend="neo4j")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return Neo4jGraphStore(
        uri,
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        database=database,
        username=username,
        password=password,
        config_source=ConfigSourceProducer.get_cached("default"),
    )
