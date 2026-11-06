"""Neo4j GraphStore driver-stub tests: codec, Scope parameters, errors, close.

A driver stub proves the store's Cypher/parameter discipline and error mapping; it does
not claim to prove transactions or real-cluster behavior (that requires integration).
"""

import json

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.graph_impl.neo4j_graph_store import Neo4jGraphStore
from jiuwen_memory.storage.types import Edge, Node

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    @staticmethod
    def __exit__(*exc):
        return False

    def begin_transaction(self):
        self.driver.transactions += 1
        return self

    def commit(self):
        self.driver.commits += 1

    def run(self, query, **params):
        self.driver.queries.append((query, params))
        if query.startswith(("MERGE (g:MemGraphLock", "CREATE CONSTRAINT", "RETURN 1")):
            return []
        return self.driver.responses.pop(0) if self.driver.responses else []


class RaisingSession(FakeSession):
    def run(self, query, **params):
        raise OSError("cluster unavailable")


class FakeDriver:
    def __init__(self, responses=None, session_cls=FakeSession):
        self.responses = list(responses or [])
        self.queries = []
        self.closed = False
        self.transactions = 0
        self.commits = 0
        self.session_cls = session_cls

    def session(self, database=None):
        return self.session_cls(self)

    def close(self):
        self.closed = True


SCOPE = Scope(org="org", space="space", user="user")


def _store(responses=None, session_cls=FakeSession) -> tuple[Neo4jGraphStore, FakeDriver]:
    driver = FakeDriver(responses, session_cls=session_cls)
    store = Neo4jGraphStore("bolt://localhost:7687", username="u", password="p", driver=driver)
    return store, driver


def test_neo4j_backend_registers_without_importing_the_driver():
    import jiuwen_memory.storage.graph_impl.neo4j_graph_store as module

    # The driver is imported lazily inside `_driver`; the module must not carry a
    # top-level `neo4j` name, so registration survives a missing optional dependency.
    assert not hasattr(module, "neo4j")
    assert Neo4jGraphStore.supports_incident_edges
    assert Neo4jGraphStore.supports_conditional_projection


def test_node_codec_roundtrip():
    node = Node("a", "memory-reference", {"ref": {"unit_id": "u"}, "content": "needle"})
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    params = Neo4jGraphStore._node_params(node)
    assert params["content"] == "needle"
    assert json.loads(params["props"]) == {"ref": {"unit_id": "u"}}
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    restored = Neo4jGraphStore._node_from_record(
        {"id": "a", "label": "memory-reference", "content": "needle", "props": params["props"]}
    )
    assert restored.properties["ref"] == {"unit_id": "u"}
    assert restored.properties["content"] == "needle"


def test_edge_codec_roundtrip():
    edge = Edge("e", "a", "b", "supports", {"revision": 3})
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    params = Neo4jGraphStore._edge_params(edge)
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    restored = Neo4jGraphStore._edge_from_record(
        {"id": "e", "source": "a", "target": "b", "relation": "supports", "props": params["props"]}
    )
    assert restored.properties["revision"] == 3
    assert restored.source == "a"
    assert restored.target == "b"


def test_scope_is_parameterized_not_interpolated():
    store, driver = _store(responses=[[], []])
    store.insert(SCOPE, nodes=[Node("n1", properties={"content": "hi"})])
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    expected = Neo4jGraphStore._scope_key(SCOPE)
    assert json.loads(expected)["org"] == "org"
    for query, params in driver.queries:
        if not params:
            continue
        assert "$sk" in query
        assert params["sk"] == expected
        # The concrete scope value must never be baked into the Cypher text.
        assert "org:space:user" not in query


def test_insert_duplicate_conflicts():
    store, _ = _store(responses=[[{"id": "n1"}]])
    with pytest.raises(ConflictError):
        store.insert(SCOPE, nodes=[Node("n1")])


def test_update_missing_raises_not_found():
    store, _ = _store(responses=[[]])
    with pytest.raises(NotFoundError):
        store.update(SCOPE, nodes=[Node("missing")])


def test_backend_errors_map_to_backend_error():
    store, _ = _store(session_cls=RaisingSession)
    with pytest.raises(BackendError):
        store.insert(SCOPE, nodes=[Node("n1")])


def test_close_releases_the_driver_idempotently():
    store, driver = _store()
    store.close()
    assert driver.closed
    driver.closed = False
    store.close()  # driver already released; no-op
    assert not driver.closed


def test_missing_driver_reports_clear_error(monkeypatch):
    # Simulate the optional dependency being absent while still allowing registration.
    store = Neo4jGraphStore("bolt://localhost:7687", username="u", password="p")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "neo4j":
            raise ImportError("no neo4j")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(BackendError, match="neo4j driver not installed"):
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        _ = store._driver


def test_multi_node_write_uses_one_transaction():
    store, driver = _store()
    store.insert(SCOPE, nodes=[Node("a"), Node("b")])
    assert driver.transactions == 1
    assert driver.commits == 1


def test_scope_keys_cannot_collide_through_delimiters_or_empty_values():
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    assert Neo4jGraphStore._scope_key(Scope(org="a:b", space="c")) != (
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        Neo4jGraphStore._scope_key(Scope(org="a", space="b:c"))
    )
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    assert Neo4jGraphStore._scope_key(Scope(org="a", user="_")) != (
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        Neo4jGraphStore._scope_key(Scope(org="a"))
    )


def test_projection_replay_after_tombstone_is_rejected():
    store, driver = _store(responses=[[{"revision": 3, "payload": "null"}]])
    with pytest.raises(ConflictError):
        store.project_edge(SCOPE, Edge("r", "a", "b", "supports"), revision=2)
    assert driver.commits == 0
    assert not any("CREATE (s)-" in q for q, _ in driver.queries)


def test_revoke_missing_edge_still_persists_tombstone():
    store, driver = _store()
    store.unproject_edge(SCOPE, "r", revision=3)
    assert driver.commits == 1
    writes = [p for q, p in driver.queries if "SET p.revision" in q]
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    assert writes == [{"sk": store._scope_key(SCOPE), "id": "r",
                       "revision": 3, "payload": "null"}]


def test_failed_endpoint_move_does_not_commit_deletion():
    store, driver = _store(responses=[[{"id": "r"}], [], []])
    with pytest.raises(NotFoundError):
        store.update(SCOPE, edges=[Edge("r", "missing", "b")])
    assert driver.transactions == 1
    assert driver.commits == 0


@pytest.mark.parametrize("uri", ["bolt://host", "neo4j://host", "bolt+ssc://host"])
def test_ssl_rejects_insecure_scheme(uri):
    from jiuwen_memory.common.errors import ValidationError

    with pytest.raises(ValidationError, match="scheme"):
        Neo4jGraphStore(uri, ssl_verify=True, ssl_ca_cert="ca.pem")


def test_ssl_custom_ca_is_passed_with_explicit_encryption(monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    driver = Mock(return_value=FakeDriver())
    trust = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "neo4j", SimpleNamespace(
        GraphDatabase=SimpleNamespace(driver=driver), TrustCustomCAs=trust,
    ))
    store = Neo4jGraphStore("neo4j+s://host", ssl_verify=True, ssl_ca_cert="ca.pem")
    store.health()
    trust.assert_called_once_with("ca.pem")
    driver.assert_called_once_with(
        "neo4j://host", auth=None, encrypted=True, trusted_certificates=trust.return_value,
    )
    # Late-bound URI changes must not bypass the original TLS policy.
    monkeypatch.setattr(store, "_resolved", lambda field, fallback:
                        "bolt://host" if field == "uri" else fallback)
    from jiuwen_memory.common.errors import ValidationError
    with pytest.raises(ValidationError, match="scheme"):
        store.health()


def test_health_does_not_create_schema():
    store, driver = _store()
    store.health()
    store.health()
    assert [query for query, _ in driver.queries] == ["RETURN 1", "RETURN 1"]
    store.insert(SCOPE, nodes=[Node("n", "memory", {})])
    assert sum(q.startswith("CREATE CONSTRAINT") for q, _ in driver.queries) == 3


@pytest.mark.parametrize("params", [
    {"uri": "bolt://host", "ssl_ca_cert": "ca.pem"},
    {"uri": "bolt+s://host"},
])
def test_ssl_misconfiguration_fails_during_build(params):
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.storage.graph_impl.neo4j_graph_store import build

    with pytest.raises(ValidationError):
        build({"username": "user", "password": "password", "ssl_verify": True, **params})
