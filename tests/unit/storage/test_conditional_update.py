"""Truth CAS: independent connections race, wrong digests and scopes never overwrite."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, RetrievalPipeline, Scope, Segment
from jiuwen_memory.common.type_def.continuity import MemoryRef
from jiuwen_memory.storage.domain_store_impl.composite_domain_store import CompositeDomainStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="org", space="project")


def _domain(kv):
    manager = SimpleNamespace(kv=lambda name: kv, security=Mock())
    return CompositeDomainStore(
        manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK
    )


def test_sqlite_cas_race_across_independent_connections(tmp_path):
    path = str(tmp_path / "cas.db")
    stores = [SQLiteKVStore(path), SQLiteKVStore(path)]
    stores[0].insert(SCOPE, "k", b"old")
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait()
        return stores[index].compare_and_swap(SCOPE, "k", b"old", str(index).encode())

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        assert sum(outcomes) == 1
        assert stores[0].get(SCOPE, "k") == str(outcomes.index(True)).encode()
        assert not stores[0].compare_and_swap(Scope(org="other"), "k", b"old", b"new")
        assert not stores[0].compare_and_swap(SCOPE, "missing", b"old", b"new")
    finally:
        for store in stores:
            store.close()


def test_domain_conditional_update_checks_digest_and_scope(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "domain.db"))
    domain = _domain(kv)
    original = MemoryUnit(id="m", scope=SCOPE, segments=[Segment(content="before")])
    changed = replace(original, segments=[Segment(content="after")])
    try:
        domain.add(SCOPE, [original])
        digest = MemoryRef.of(original).content_digest
        assert domain.supports_conditional_update
        assert domain.update_if_unchanged(SCOPE, changed, digest)
        assert not domain.update_if_unchanged(SCOPE, original, digest)
        assert domain.get(SCOPE, ["m"])[0].content == "after"
        with pytest.raises(ValidationError):
            domain.update_if_unchanged(Scope(org="other"), changed, digest)
    finally:
        kv.close()
