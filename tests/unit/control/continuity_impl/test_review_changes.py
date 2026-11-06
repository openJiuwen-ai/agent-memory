"""Change journal and incremental review propagation over relation revisions."""

import pytest

from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord

pytestmark = pytest.mark.unit


def test_relation_change_is_journaled_and_queryable(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    snapshot = service.review_changes(scope, 0)
    assert snapshot["watermark"] == 1
    assert [c["relation_id"] for c in snapshot["changes"]] == ["r"]
    assert snapshot["changes"][0]["target_key"] == b.key
    # Incremental: nothing new since the watermark.
    assert service.review_changes(scope, snapshot["watermark"])["changes"] == []


def test_revision_is_journaled_beyond_first_commit(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    first = RelationRecord("r", a, b, "supports", status="candidate")
    service.relation(scope, first, 0, read, write)
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a], revision=2),
        1, read, write,
    )
    snapshot = service.review_changes(scope, 0)
    assert snapshot["watermark"] == 2
    assert [c["revision"] for c in snapshot["changes"]] == [1, 2]


def test_propagate_review_aggregates_three_tiers(setup):
    service, scope, write, read, _, _ = setup
    a, b, c = [MemoryRef.of(write(t)[0]) for t in ("anchor", "supported", "conclusion")]
    service.relation(
        scope,
        RelationRecord("r1", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    service.relation(
        scope,
        RelationRecord("r2", b, c, "supports", status="confirmed", evidence_refs=[b]),
        0, read, write,
    )
    result = service.propagate_review(scope, 0, read, required={c.key})
    assert result["required"] == [c.key]
    assert result["candidate"] == [b.key]
    assert result["gaps"] == []
    assert result["watermark"] == 2


def test_propagate_review_is_incremental_after_watermark(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    first = service.propagate_review(scope, 0, read, required=set())
    assert first["watermark"] == 1
    assert first["candidate"] == [b.key]
    second = service.propagate_review(scope, first["watermark"], read, required=set())
    assert second["required"] == [] and second["candidate"] == []
    assert second["watermark"] == 1


def test_journal_repairs_watermark_after_partial_write(setup):
    from jiuwen_memory.common.type_def.continuity import fingerprint

    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    relation = RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a])
    # Simulate a crash between the record write and the watermark write: the record
    # exists but the watermark was never advanced.
    key = fingerprint({"relation_id": "r", "revision": relation.revision})
    service.repository.save(
        scope, "changes", key,
        {
            "change_id": 0,
            "relation_id": "r",
            "revision": relation.revision,
            "relation_type": "supports",
            "status": "confirmed",
            "source_key": a.key,
            "target_key": b.key,
            "changed_at": "",
        },
    )
    # The retry must repair the watermark instead of returning early.
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    service._journal_change(scope, relation)
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    assert service._change_watermark(scope)["next"] == 1
    # A subsequent change must not reuse change id 0.
    service.relation(
        scope,
        RelationRecord("r2", b, a, "contradicts", status="confirmed", evidence_refs=[b]),
        0, read, write,
    )
    snapshot = service.review_changes(scope, 0)
    change_ids = [c["change_id"] for c in snapshot["changes"]]
    assert snapshot["watermark"] == 2
    assert change_ids == sorted(change_ids)
    assert len(change_ids) == len(set(change_ids))


def test_journal_recovery_is_order_independent(setup):
    from jiuwen_memory.common.type_def.continuity import fingerprint

    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    rel_a = RelationRecord("ra", a, b, "supports", status="confirmed", evidence_refs=[a])
    # Simulate: A's record was saved but its watermark write crashed (id 0 unrecorded).
    key_a = fingerprint({"relation_id": "ra", "revision": rel_a.revision})
    service.repository.save(
        scope, "changes", key_a,
        {
            "change_id": 0,
            "relation_id": "ra",
            "revision": rel_a.revision,
            "relation_type": "supports",
            "status": "confirmed",
            "source_key": a.key,
            "target_key": b.key,
            "changed_at": "",
        },
    )
    # A different change B is journaled before any retry of A: it must not reuse id 0.
    rel_b = RelationRecord("rb", b, a, "contradicts", status="confirmed", evidence_refs=[b])
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    service._journal_change(scope, rel_b)
    # Retrying A is idempotent; its durable id 0 must remain intact.
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    service._journal_change(scope, rel_a)
    snapshot = service.review_changes(scope, 0)
    change_ids = [c["change_id"] for c in snapshot["changes"]]
    assert snapshot["watermark"] == 2
    assert change_ids == sorted(change_ids)
    assert len(change_ids) == len(set(change_ids))


def test_read_after_watermark_save_failure_has_resumable_cursor(setup, monkeypatch):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("claim", "judgment")]
    save = service.repository.save

    def fail_watermark(scope, kind, key, record):
        if kind == "changes" and key == "watermark":
            raise OSError("watermark unavailable")
        return save(scope, kind, key, record)

    monkeypatch.setattr(service.repository, "save", fail_watermark)
    result = service.relation(
        scope, RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    assert result["status"] == "partial"
    snapshot = service.review_changes(scope)
    assert [c["change_id"] for c in snapshot["changes"]] == [0]
    assert snapshot["watermark"] == 1
    assert service.review_changes(scope, snapshot["watermark"])["changes"] == []
