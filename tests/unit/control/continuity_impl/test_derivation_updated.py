"""Explicit new-version derivation rebuild produces updated (CAS) items, not duplicates."""

from dataclasses import asdict, replace
from typing import Any, NamedTuple
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    TaskRequest,
)

pytestmark = pytest.mark.unit


class DerivationFixture(NamedTuple):
    service: Any
    scope: Any
    event: Any
    write: Any
    read: Any
    units: Any


def _derive_service(setup, contents):
    service, scope, write, read, _, units = setup
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)
    binding = service.task(
        scope,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="s"),
        "owner",
        read,
    )
    service.derivation_extractor = Mock()
    service.derivation_extractor.extract.side_effect = [
        [MemoryUnit(id=f"v{i}", scope=scope, segments=[Segment(content=t)])
         for i, t in enumerate(texts)]
        for texts in contents
    ]
    service.derivation_version = "extractor/model/1"
    service.derivation_dedup = Mock()
    service.derivation_dedup.recall.return_value = []
    event = ContinuityEvent(
        task["task_id"], binding["task"]["bindings"][0]["binding_id"], "event", "source"
    )
    return DerivationFixture(service, scope, event, write, read, units)


def _capabilities(units):
    def lookup(key):
        return [
            u for u in units.values()
            if u.system_metadata.get("continuity_output") == key
        ]

    def conditional_update(ref, before_digest, content, metadata):
        unit = units[ref.unit_id]
        if MemoryRef.of(unit).content_digest != before_digest:
            return False
        units[ref.unit_id] = replace(unit, segments=[Segment(content=content)],
                                     system_metadata={**unit.system_metadata, **metadata})
        return True

    return lookup, conditional_update


def test_new_version_rebuild_updates_previous_output(setup):
    service, scope, event, write, read, units = _derive_service(
        setup, [["first version"], ["second version"]]
    )
    lookup, conditional_update = _capabilities(units)

    first = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    assert first["derivation_status"] == "committed"
    assert first["derived"][0]["action"] == "created"
    previous = next(u for u in units.values() if u.system_metadata.get("continuity_build"))
    previous_id = previous.id

    # Explicit new-version rebuild: same event, new model version, revised content.
    service.derivation_version = "extractor/model/2"
    service.derivation_dedup.recall.return_value = [(previous, 0.9)]
    second = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    assert second["derivation_status"] == "committed"
    assert second["derived"][0]["action"] == "updated"
    # The previous output is updated in place, not duplicated.
    assert units[previous_id].segments[0].content == "second version"
    outputs = [u for u in units.values() if u.system_metadata.get("continuity_build")]
    assert [u.id for u in outputs] == [previous_id]


def test_rebuild_without_cas_fails_closed_as_partial(setup):
    service, scope, event, write, read, units = _derive_service(
        setup, [["first version"], ["second version"]]
    )
    lookup, conditional_update = _capabilities(units)
    service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    previous = next(u for u in units.values() if u.system_metadata.get("continuity_build"))

    service.derivation_version = "extractor/model/2"
    service.derivation_dedup.recall.return_value = [(previous, 0.9)]
    # No CAS callback: the revision can never be auto-applied.
    second = service.record(scope, event, "wb", read, write, query_output=lookup)
    assert second["derivation_status"] == "partial"
    assert second["derivation_conflicts"]


def test_failed_rebuild_does_not_commit_stale_plan(setup):
    service, scope, event, write, read, units = _derive_service(
        setup, [["first version"]]
    )
    lookup, conditional_update = _capabilities(units)
    service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    previous = next(u for u in units.values() if u.system_metadata.get("continuity_build"))

    # New version, but the model fails. The batch id must not be persisted ahead of a
    # successful plan, so a retry re-plans instead of replaying the stale plan.
    service.derivation_version = "extractor/model/2"
    service.derivation_dedup.recall.return_value = [(previous, 0.9)]
    extractor = Mock()
    extractor.extract.side_effect = RuntimeError("model unavailable")
    service.derivation_extractor = extractor
    failed = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    assert failed["derivation_status"] == "partial"
    assert units[previous.id].segments[0].content == "first version"  # unchanged

    # The model recovers; the retry must re-plan and update, not reuse the stale plan.
    extractor.extract.side_effect = [
        [MemoryUnit(id="v2", scope=scope, segments=[Segment(content="second version")])]
    ]
    second = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    assert second["derivation_status"] == "committed"
    assert second["derived"][0]["action"] == "updated"
    assert units[previous.id].segments[0].content == "second version"


def test_two_outputs_do_not_update_the_same_old_record(setup):
    service, scope, event, write, read, units = _derive_service(
        setup, [["old a", "old b"]]
    )
    lookup, conditional_update = _capabilities(units)
    service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    old_outputs = [u for u in units.values() if u.system_metadata.get("continuity_build")]
    assert len(old_outputs) == 2
    old = old_outputs[0]

    # New version emits two outputs, but recall matches BOTH to the same old record: the
    # second must not freeze a duplicate update of the same digest (fall back to created).
    service.derivation_version = "extractor/model/2"
    service.derivation_dedup.recall.return_value = [(old, 0.9)]
    service.derivation_extractor.extract.side_effect = [
        [
            MemoryUnit(id="v1", scope=scope, segments=[Segment(content="new a")]),
            MemoryUnit(id="v2", scope=scope, segments=[Segment(content="new b")]),
        ]
    ]
    second = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=conditional_update,
    )
    assert second["derivation_status"] == "committed"
    actions = [d["action"] for d in second["derived"]]
    assert actions.count("updated") == 1
    assert actions.count("created") == 1


@pytest.mark.parametrize("legacy_tags", [False, True])
def test_updated_output_is_recoverable_after_ack_loss(setup, legacy_tags):
    service, scope, event, write, read, units = _derive_service(
        setup, [["first version"], ["second version"]]
    )
    lookup, _ = _capabilities(units)

    def apply_then_lost(ref, before_digest, content, metadata):
        # The CAS applies the in-place update, then the confirmation is lost.
        unit = units[ref.unit_id]
        if MemoryRef.of(unit).content_digest != before_digest:
            return False
        units[ref.unit_id] = replace(unit, segments=[Segment(content=content)],
                                     system_metadata=(unit.system_metadata if legacy_tags
                                                      else {**unit.system_metadata, **metadata}))
        raise OSError("update ack lost")

    first = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=apply_then_lost,
    )
    assert first["derivation_status"] == "committed"
    previous = next(u for u in units.values() if u.system_metadata.get("continuity_build"))
    previous_id = previous.id

    service.derivation_version = "extractor/model/2"
    service.derivation_dedup.recall.return_value = [(previous, 0.9)]
    second = service.record(
        scope, event, "wb", read, write, query_output=lookup,
        conditional_update=apply_then_lost,
    )
    assert second["derivation_status"] == "partial"
    assert units[previous_id].segments[0].content == "second version"  # update landed

    # Both legacy tags and atomically refreshed tags use the frozen target and
    # post-update digest for recovery, rather than created-output matching.
    target = MemoryRef.of(units[previous_id])
    recovered = service.recover(
        scope, ContinuityRecovery("derivation", second["operation_id"], [target]), read
    )
    assert recovered["next_action"] == "retry_original_event"
    assert recovered["derived"][0]["action"] == "updated"
    assert recovered["derived"][0]["ref"] == asdict(target)
    lookup, update = _capabilities(units)
    completed = service.record(
        scope, event, "wb", read, write, query_output=lookup, conditional_update=update,
    )
    assert completed["derivation_status"] == "committed"
    assert units[previous_id].system_metadata["continuity_build"] == completed["build_id"]
    assert units[previous_id].system_metadata["continuity_output"] == (
        completed["derived"][0]["output_key"]
    )
    assert len(units) == 2  # original event + the same derived record


@pytest.mark.parametrize("changed", [False, True])
def test_recovery_reused_requires_frozen_digest(setup, changed):
    from jiuwen_memory.common.errors import ConflictError
    from jiuwen_memory.control.continuity_impl.derivation import DerivationItem

    service, scope, write, read, _, units = setup
    source, target = [write(text)[0] for text in ("source", "original")]
    frozen = MemoryRef.of(target)
    receipt = {
        "refs": [asdict(MemoryRef.of(source))],
        "build_id": "new-build",
        "build_plan": [asdict(DerivationItem("output", "reused", ref=frozen))],
    }
    service.repository.save(scope, "events", "operation", receipt)
    if changed:
        units[target.id] = replace(target, segments=[Segment(content="changed by another writer")])
    current = MemoryRef.of(units[target.id])
    request = ContinuityRecovery("derivation", "operation", [current])
    if changed:
        with pytest.raises(ConflictError, match="frozen reference"):
            service.recover(scope, request, read)
        assert service.repository.get(scope, "events", "operation") == receipt
    else:
        result = service.recover(scope, request, read)
        assert result["derived"] == [
            {"output_key": "output", "action": "reused", "ref": asdict(frozen)}
        ]


@pytest.mark.parametrize("segments", [
    [Segment(content="one"), Segment(content="two")],
    [Segment(content="image caption", assets=["asset://image"])],
])
def test_rebuild_preserves_structured_target(setup, segments):
    service, scope, event, write, read, units = _derive_service(setup, [["old"], ["new"]])
    lookup, update = _capabilities(units)
    service.record(scope, event, "wb", read, write, query_output=lookup,
                   conditional_update=update)
    previous = next(u for u in units.values() if u.system_metadata.get("continuity_build"))
    previous = replace(previous, segments=segments)
    units[previous.id] = previous
    service.derivation_version = "v2"
    service.derivation_dedup.recall.return_value = [(previous, 0.9)]
    result = service.record(scope, event, "wb", read, write, query_output=lookup,
                            conditional_update=update)
    assert result["derived"][0]["action"] == "created"
    assert units[previous.id].segments == segments
