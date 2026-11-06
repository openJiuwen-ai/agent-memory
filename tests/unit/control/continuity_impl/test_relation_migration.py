"""Relation truth migration: legacy `ref` heads → Registry KV heads + revision history."""

import importlib.util
from dataclasses import asdict
from pathlib import Path

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import (
    MemoryRef,
    RelationRecord,
    canonical,
    fingerprint,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="project")


def _load_module():
    path = Path(__file__).resolve().parents[4] / "scripts" / "migrate_relations_to_kv.py"
    spec = importlib.util.spec_from_file_location("migrate_relations_to_kv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store_relation(memory_kv, relation: RelationRecord) -> MemoryRef:
    unit = MemoryUnit(
        id=f"rel-{relation.relation_id}-r{relation.revision}",
        scope=_SCOPE,
        segments=[Segment(content=canonical(asdict(relation)))],
    )
    memory_kv.insert(_SCOPE, memory_key(unit.id), dumps(unit))
    return MemoryRef.of(unit)


def _legacy_head(memory_kv, current: RelationRecord, history: list[RelationRecord]) -> dict:
    return {
        "revision": current.revision,
        "digest": fingerprint(asdict(current)),
        "status": "committed",
        "payload_digest": fingerprint(asdict(current)),
        "ref": asdict(_store_relation(memory_kv, current)),
        "history": [asdict(_store_relation(memory_kv, item)) for item in history],
    }


def test_migration_rewrites_head_and_carries_history(tmp_path):
    module = _load_module()
    memory_kv = SQLiteKVStore(str(tmp_path / "memory.db"))
    registry_kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        a = MemoryRef(_SCOPE, "a", "0" * 64)
        b = MemoryRef(_SCOPE, "b", "1" * 64)
        first = RelationRecord("r1", a, b, "supports", status="candidate", revision=1)
        second = RelationRecord(
            "r1", a, b, "supports", status="confirmed", evidence_refs=[a], revision=2
        )
        head = _legacy_head(memory_kv, second, [first])
        relation_id, new_head, history, missing = module._migrate_head(memory_kv, head)

        assert relation_id == "r1"
        assert new_head["revision"] == 2
        assert new_head["status"] == "projecting"
        assert new_head["current"]["status"] == "confirmed"
        assert new_head["payload_digest"] == fingerprint(new_head["current"])
        assert [revision for revision, _ in history] == [1]
        assert history[0][1]["status"] == "candidate"
        assert missing == []

        # Persist through the Registry wrapper (the migration must not touch KVStore directly).
        repository = ContinuityRepository(registry_kv, single_writer=True, allow_ephemeral=True)
        try:
            for revision, payload in history:
                repository.save(_SCOPE, "relations", f"{relation_id}/rev/{revision}", payload)
            repository.save(_SCOPE, "relations", relation_id, new_head)
            stored = repository.list(_SCOPE, "relations")
        finally:
            repository.close()
        heads = [record for record in stored if "current" in record]
        revisions = [
            record
            for record in stored
            if "relation_id" in record and "current" not in record
        ]
        assert len(heads) == 1 and heads[0]["revision"] == 2
        assert len(revisions) == 1 and revisions[0]["revision"] == 1
    finally:
        memory_kv.close()
        registry_kv.close()


def test_migrated_heads_are_skipped_on_a_second_run(tmp_path):
    module = _load_module()
    memory_kv = SQLiteKVStore(str(tmp_path / "memory.db"))
    registry_kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    try:
        a = MemoryRef(_SCOPE, "a", "0" * 64)
        b = MemoryRef(_SCOPE, "b", "1" * 64)
        current = RelationRecord("r1", a, b, "supports", status="confirmed",
                                 evidence_refs=[a], revision=1)
        head = _legacy_head(memory_kv, current, [])
        relation_id, new_head, history, missing = module._migrate_head(memory_kv, head)
        repository = ContinuityRepository(registry_kv, single_writer=True,
                                          allow_ephemeral=True)
        try:
            assert history == [] and missing == []
            repository.save(_SCOPE, "relations", relation_id, new_head)
            pending = [
                record
                for record in repository.list(_SCOPE, "relations")
                if "current" not in record and record.get("ref")
            ]
        finally:
            repository.close()
        # The second run finds nothing to migrate: the head now carries `current`.
        assert pending == []
    finally:
        memory_kv.close()
        registry_kv.close()


def test_missing_history_is_reported_not_swallowed(tmp_path):
    module = _load_module()
    memory_kv = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        a = MemoryRef(_SCOPE, "a", "0" * 64)
        b = MemoryRef(_SCOPE, "b", "1" * 64)
        current = RelationRecord("r1", a, b, "supports", status="confirmed",
                                 evidence_refs=[a], revision=2)
        head = _legacy_head(memory_kv, current, [])
        head["history"] = [{"scope": asdict(_SCOPE), "unit_id": "gone", "content_digest": "0" * 64}]
        _relation_id, _new_head, history, missing = module._migrate_head(memory_kv, head)
        assert history == []
        assert missing, "a lost historical revision must be reported"
    finally:
        memory_kv.close()


def test_pending_projection_includes_uncommitted_heads(tmp_path):
    module = _load_module()
    registry_kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    repository = ContinuityRepository(registry_kv, single_writer=True, allow_ephemeral=True)
    try:
        repository.save(_SCOPE, "relations", "r1",
                        {"revision": 1, "status": "projecting", "current": {"relation_id": "r1"}})
        repository.save(_SCOPE, "relations", "r2",
                        {"revision": 1, "status": "committed", "current": {"relation_id": "r2"}})
        pending = module._pending_projection(repository)
        assert [head["current"]["relation_id"] for _scope, head in pending] == ["r1"]
    finally:
        repository.close()
        registry_kv.close()


def test_incomplete_history_keeps_the_legacy_head(tmp_path):
    """A head with unreadable history must not be overwritten (it would be skipped forever)."""
    module = _load_module()
    assert module._blocks_head_write(["broken ref"], allow_incomplete_history=False) is True
    assert module._blocks_head_write(["broken ref"], allow_incomplete_history=True) is False
    assert module._blocks_head_write([], allow_incomplete_history=False) is False


def test_projection_failure_is_reported_and_left_pending(tmp_path):
    module = _load_module()
    registry_kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    repository = ContinuityRepository(registry_kv, single_writer=True, allow_ephemeral=True)
    try:
        repository.save(
            _SCOPE,
            "relations",
            "r1",
            {
                "revision": 1,
                "status": "projecting",
                "current": asdict(
                    RelationRecord(
                        "r1",
                        MemoryRef(_SCOPE, "a", "0" * 64),
                        MemoryRef(_SCOPE, "b", "1" * 64),
                        "supports",
                        status="confirmed",
                        evidence_refs=[MemoryRef(_SCOPE, "a", "0" * 64)],
                        revision=1,
                    )
                ),
            },
        )

        class BrokenProjector:
            @staticmethod
            def build(_units, mode=None):
                raise RuntimeError("graph unavailable")

        failures = module._reproject(
            None, {"graph_store": "default"}, repository, projector=BrokenProjector()
        )
        assert failures and "r1" in failures[0]
        head = repository.get(_SCOPE, "relations", "r1")
        assert head["status"] == "projecting"
        pending = module._pending_projection(repository)
        assert [record["current"]["relation_id"] for _s, record in pending] == ["r1"]
    finally:
        repository.close()
        registry_kv.close()


def test_maintenance_ports_share_the_assembly_context_and_close(tmp_path):
    """维护句柄必须带出同一份装配上下文，并在 close 时释放装配创建的资源。"""
    from jiuwen_memory.api import assemble_maintenance_ports

    ports = assemble_maintenance_ports(
        {
            "engine": {"default": {"target": "cloud", "params": {}}},
            "kv_store": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "memory.db"), "fixed_path": True},
                },
                "registry": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "registry.db"), "fixed_path": True},
                },
            },
        },
        registry_store="registry",
    )
    try:
        assert ports.ctx is not None, "投影器要复用同一装配上下文"
        assert ports.truth_kv is not ports.registry_kv
    finally:
        ports.close()
    # close 幂等：重复调用不抛。
    ports.close()
