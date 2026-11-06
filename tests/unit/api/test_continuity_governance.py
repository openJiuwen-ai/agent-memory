"""Source governance through the public API: dynamic quarantine blocks reads per range."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from jiuwen_memory.api import (
    ContinuityEvent,
    MemoryRef,
    PermissionDeniedError,
    RoleViewQuery,
    Scope,
    SpaceMember,
    SpaceSpec,
    TaskRequest,
    assemble_runtime,
)
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


class RuntimeFixture(NamedTuple):
    runtime: Any
    api: Any
    project: Any
    owner: Any
    ops: Any
    config: Any


def _security(actor, role=Role.USER):
    return internal_context(ScopedAuthenticator(actor, role=role))


def _runtime(tmp_path):
    config = yaml.safe_load(Path("examples/role_memory/server.yaml").read_text())
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    runtime = assemble_runtime(config=config)
    api = runtime.api
    project = Scope(org="demo", space="role-project")
    owner = Scope(org="demo", user="owner")
    ops = Scope(org="demo", user="ops")
    api.create_space(
        SpaceSpec(org="demo", space="role-project", owner=owner),
        security=_security(ops, Role.ADMIN),
    )
    for name, value in config["globals"]["continuity_connectors"].items():
        api.add_space_member(
            "demo", "role-project", SpaceMember(scope=Scope(**value["actor"])),
            security=_security(owner),
        )
    return RuntimeFixture(runtime, api, project, owner, ops, config)


def _gov(ops):
    return _security(ops, Role.ADMIN)


def test_quarantined_source_is_blocked_without_leaking_to_other_sources(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.role_task(project, TaskRequest("create", "create"), security=_security(owner))
        bindings = {}
        for name in actors:
            task = api.role_task(
                project,
                TaskRequest(
                    "bind", name, task["task_id"], task["revision"],
                    connector_id=name, context_id="session",
                ),
                security=_security(owner),
            )
            bindings[name] = task["task"]["bindings"][-1]["binding_id"]
        workbuddy_binding = bindings.get("workbuddy")
        hermes_binding = bindings.get("hermes")
        assert workbuddy_binding is not None, "WorkBuddy binding was not registered"
        assert hermes_binding is not None, "Hermes binding was not registered"
        event = ContinuityEvent(
            task["task_id"], workbuddy_binding, "wb-event", "workbuddy fact"
        )
        wb_receipt = api.role_record(project, event, security=_security(actors["workbuddy"]))
        event2 = ContinuityEvent(task["task_id"], hermes_binding, "h-event", "hermes fact")
        api.role_record(project, event2, security=_security(actors["hermes"]))

        wb_ref = MemoryRef.parse(wb_receipt["refs"][0])
        reader = _security(actors["hermes"])
        assert api.get(wb_ref.unit_id, wb_ref.scope, security=reader).content == "workbuddy fact"

        # Dynamic quarantine with reason; only the workbuddy range becomes unavailable.
        api.role_quarantine_source(project, "workbuddy", "injected bad results", security=_gov(ops))
        with pytest.raises(PermissionDeniedError):
            api.get(wb_ref.unit_id, wb_ref.scope, security=reader)
        view = api.role_view(project, RoleViewQuery(task_id=task["task_id"]), security=reader)
        assert "workbuddy fact" not in str(view)
        # Other sources remain readable in the same project.
        assert "hermes fact" in str(view)

        # Release restores the source.
        assert api.role_release_source(project, "workbuddy", security=_gov(ops)) is True
        assert api.get(wb_ref.unit_id, wb_ref.scope, security=reader).content == "workbuddy fact"
    finally:
        runtime.close()


def test_quarantine_entry_is_governed(tmp_path):
    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    try:
        # A non-governance actor cannot isolate or release a source range.
        with pytest.raises(PermissionDeniedError):
            api.role_quarantine_source(project, "workbuddy", "reason", security=_security(owner))
        assert api.role_release_source(project, "workbuddy", security=_gov(ops)) is False
    finally:
        runtime.close()


def test_cutover_entry_is_governed_and_records_watermark(tmp_path):
    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    try:
        with pytest.raises(PermissionDeniedError):
            api.role_mark_source_cutover(project, "workbuddy", "session", security=_security(owner))
        result = api.role_mark_source_cutover(project, "workbuddy", "session", security=_gov(ops))
        assert result["cutover_position"] == 0
    finally:
        runtime.close()


def test_publish_copies_verbatim_with_provenance_and_reason(tmp_path):
    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    private = Scope(org="demo", space="private")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="private", owner=owner), security=_gov(ops)
        )
        source = api.add("private insight", private, security=_security(owner))[0]
        result = api.role_publish(project, MemoryRef.of(source), "share with team",
                                  security=_security(owner))
        ref = MemoryRef.parse(result["ref"])
        shared = api.get(ref.unit_id, ref.scope, security=_security(owner))
        assert shared.content == "private insight"  # verbatim, never auto-summarized
        assert shared.system_metadata["continuity_kind"] == "statement"
        assert shared.system_metadata["continuity_publish_reason"] == "share with team"
        assert "continuity_published_source" in shared.system_metadata
    finally:
        runtime.close()


def test_publish_requires_reason_and_is_governed(tmp_path):
    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError):
            api.role_publish(project, MemoryRef(project, "missing", "a" * 64), "   ",
                             security=_security(owner))
    finally:
        runtime.close()


def test_publish_requires_share_not_just_read(tmp_path):
    from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole

    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    private = Scope(org="demo", space="private")
    alice = Scope(org="demo", user="alice")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="private", owner=owner), security=_gov(ops)
        )
        # alice can WRITE the shared project and READ the private source, but has no
        # governance SHARE on the source — publishing must be denied.
        api.add_space_member(
            "demo", "private",
            SpaceMember(scope=alice, content_role=SpaceContentRole.VIEWER,
                        governance_role=SpaceGovernanceRole.NONE),
            security=_security(owner),
        )
        api.add_space_member(
            "demo", "role-project",
            SpaceMember(scope=alice, content_role=SpaceContentRole.CONTRIBUTOR,
                        governance_role=SpaceGovernanceRole.NONE),
            security=_security(owner),
        )
        source = api.add("private insight", private, security=_security(owner))[0]
        assert api.get(source.id, private, security=_security(alice)).content == "private insight"
        with pytest.raises(PermissionDeniedError):
            api.role_publish(project, MemoryRef.of(source), "leak", security=_security(alice))
    finally:
        runtime.close()


def test_public_record_derives_and_enforces_source_dependency(tmp_path):
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        actor = Scope(**config["globals"]["continuity_connectors"]["workbuddy"]["actor"])
        task = api.role_task(project, TaskRequest("create", "create"), security=_security(owner))
        bound = api.role_task(project, TaskRequest(
            "bind", "bind", task["task_id"], 1, connector_id="workbuddy", context_id="session"
        ), security=_security(owner))
        extractor = Mock()
        extractor.extract.return_value = [MemoryUnit(
            id="proposed", scope=project, segments=[Segment(content="derived fact")]
        )]
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity.derivation_extractor = extractor
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity.derivation_version = "test/1"
        event = ContinuityEvent(task["task_id"], bound["task"]["bindings"][0]["binding_id"],
                                "event", "source event")
        receipt = api.role_record(project, event, security=_security(actor))
        assert receipt["derivation_status"] == "committed"
        output = MemoryRef.parse(receipt["derived"][0]["ref"])
        derived = api.get(output.unit_id, output.scope, security=_security(actor))
        assert derived.content == "derived fact"
        replay = api.role_record(project, event, security=_security(actor))
        assert replay["derived"] == receipt["derived"]
        extractor.extract.assert_called_once()
        api.role_quarantine_source(project, "workbuddy", "test quarantine", security=_gov(ops))
        with pytest.raises(PermissionDeniedError):
            api.get(output.unit_id, output.scope, security=_security(actor))
    finally:
        runtime.close()


def test_cross_space_source_quarantine_uses_trusted_recall_origin(tmp_path, monkeypatch):
    from jiuwen_memory.api import Context
    from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem

    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    other = Scope(org=project.org, space="other-project")
    try:
        api.create_space(
            SpaceSpec(org=other.org, space=other.space, owner=owner),
            security=_gov(ops),
        )
        api.role_quarantine_source(project, "workbuddy", "invalid source", security=_gov(ops))

        async def recall(scope, query):
            return RetrievalResult(items=[RetrievedItem(
                unit_id=scope.space, content=scope.space,
                system_metadata={"continuity_connector": "workbuddy",
                                 "space": other.space},
            )])

        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        monkeypatch.setattr(api._queries, "recall", recall)
        result = api.search(
            "evidence", Context(Scope(org=project.org), extensions={
                "spaces": [project.space, other.space],
            }), security=_security(owner),
        )
        assert [item.unit_id for item in result.items] == [other.space]
        assert any(error.error_type == "dependency_check_required" for error in result.errors)
    finally:
        runtime.close()


def test_role_record_persists_event_time(tmp_path):
    runtime, api, project, owner, _, config = _runtime(tmp_path)
    connector, settings = next(iter(config["globals"]["continuity_connectors"].items()))
    actor = Scope(**settings["actor"])
    try:
        task = api.role_task(project, TaskRequest("create", "create"), security=_security(owner))
        task = api.role_task(project, TaskRequest(
            "bind", "bind", task["task_id"], task["revision"],
            connector_id=connector, context_id="time-test",
        ), security=_security(owner))
        event = ContinuityEvent(
            task["task_id"], task["task"]["bindings"][0]["binding_id"], "event", "old observation",
            occurred_at="2024-02-03T12:00:00+08:00",
        )
        result = api.role_record(project, event, security=_security(actor))
        unit = api.get(result["refs"][0]["unit_id"], project, security=_security(actor))
        assert unit.temporal.t_event == datetime.fromisoformat(event.occurred_at)
    finally:
        runtime.close()


def test_old_update_plan_cannot_erase_assets(tmp_path):
    from dataclasses import asdict
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def.continuity import fingerprint
    from jiuwen_memory.control.continuity_impl.derivation import DerivationItem

    runtime, api, project, owner, _, config = _runtime(tmp_path)
    actor = Scope(**config["globals"]["continuity_connectors"]["workbuddy"]["actor"])
    try:
        task = api.role_task(project, TaskRequest("create", "create"), security=_security(owner))
        bound = api.role_task(project, TaskRequest(
            "bind", "bind", task["task_id"], 1, connector_id="workbuddy", context_id="session",
        ), security=_security(owner))
        target = api.add(
            "caption", project, assets=["asset://original"], security=_security(actor),
        )[0]
        ref = MemoryRef.of(target)

        event = ContinuityEvent(task["task_id"], bound["task"]["bindings"][0]["binding_id"],
                                "event", "source")
        original = api.role_record(project, event, security=_security(actor))
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        service = api._continuity
        key = original["operation_id"]
        # Simulate a durable plan frozen by the older planner, then resume through
        # the real record/reconcile/API CAS chain (do not replace record itself).
        receipt = service.repository.get(project, "events", key)
        receipt.update(
            build_id=fingerprint({"event": key, "version": "legacy"}),
            build_plan=[asdict(DerivationItem(
                "legacy-output", "updated", ref=ref, content="replacement",
                digest_before=ref.content_digest, digest_after="not-applied",
            ))],
            derived=[], derivation_status="partial",
        )
        service.repository.save(project, "events", key, receipt)
        service.derivation_version = "legacy"
        service.derivation_extractor = Mock()
        service.derivation_extractor.extract.side_effect = AssertionError("must reuse frozen plan")
        result = api.role_record(project, event, security=_security(actor))
        assert result["status"] == "committed"
        assert result["derivation_status"] == "partial"
        assert service.repository.get(project, "events", key)["derivation_status"] == "partial"
        unchanged = api.get(target.id, project, security=_security(actor))
        assert unchanged.segments == target.segments
    finally:
        runtime.close()


@pytest.mark.parametrize("metadata", [
    {"continuity_envelope": '{"occurred_at":"2001-01-01T00:00:00Z"}'},
    {"continuity_envelope": 'not json'},
    {"continuity_envelope": '{"occurred_at":1}'},
    {"continuity_envelope": '{"occurred_at":"2001-01-01T00:00:00"}'},
    {"continuity_connector": "workbuddy"},
    {"continuity_kind": "relation"},
    {"continuity_event": "forged"},
    {"continuity_future_reserved": "value"},
])
@pytest.mark.parametrize("method", ["add", "batch_add"])
def test_public_writes_reject_continuity_metadata(tmp_path, metadata, method):
    from jiuwen_memory.control.types import BatchWriteItem

    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError, match="保留"):
            if method == "add":
                api.add("forged", project, system_metadata=metadata, security=_security(owner))
            else:
                api.batch_add([BatchWriteItem(content="forged")], project,
                              system_metadata=metadata, security=_security(owner))
    finally:
        runtime.close()


def test_explicit_event_time_is_independent_of_message_time(tmp_path):
    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        event = datetime(2001, 1, 1, tzinfo=UTC)
        message = datetime(2024, 2, 3, tzinfo=UTC)
        unit = api.add("observation", project, event_time=event, occurred_at=message,
                       security=_security(owner))[0]
        assert unit.temporal.t_event == event
        assert unit.temporal.t_message == message
    finally:
        runtime.close()


def test_rebuild_updates_layers_and_batch_identity_atomically(tmp_path):
    from dataclasses import replace
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def import ContentLayers, MemoryUnit, Segment

    runtime, api, project, owner, _, config = _runtime(tmp_path)
    actor = Scope(**config["globals"]["continuity_connectors"]["workbuddy"]["actor"])
    try:
        task = api.role_task(project, TaskRequest("create", "create"), security=_security(owner))
        bound = api.role_task(project, TaskRequest(
            "bind", "bind", task["task_id"], 1, connector_id="workbuddy", context_id="session",
        ), security=_security(owner))
        extractor = Mock()
        extractor.extract.side_effect = [
            [MemoryUnit(scope=project, segments=[Segment(content=content)])]
            for content in ["old body", "new body"]
        ]
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        service = api._continuity
        service.derivation_extractor = extractor
        service.derivation_version = "v1"
        event = ContinuityEvent(task["task_id"], bound["task"]["bindings"][0]["binding_id"],
                                "event", "source")
        receipt = api.role_record(project, event, security=_security(actor))
        ref = MemoryRef.parse(receipt["derived"][0]["ref"])
        old = api.get(ref.unit_id, project, security=_security(actor))
        old = replace(old, layers=ContentLayers(l0="stale summary", l1="stale detail"))
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        assert api._commands.conditional_update(old, ref.content_digest)
        service.derivation_version = "v2"
        service.derivation_dedup = Mock()
        service.derivation_dedup.recall.return_value = [(old, 0.9)]
        result = api.role_record(project, event, security=_security(actor))
        assert result["derivation_status"] == "committed"
        current = api.get(ref.unit_id, project, security=_security(actor))
        assert current.content == "new body"
        assert current.layers.l0 != "stale summary"
        assert current.layers.l1 != "stale detail"
        assert current.system_metadata["continuity_build"] == result["build_id"]
        assert current.system_metadata["continuity_output"] == result["derived"][0]["output_key"]
    finally:
        runtime.close()


@pytest.mark.parametrize("value", [1, "2001-01-01", datetime(2001, 1, 1)])
def test_event_time_requires_aware_datetime(tmp_path, value):
    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError, match="timezone-aware"):
            api.add("event", project, event_time=value, security=_security(owner))
    finally:
        runtime.close()


def test_batch_item_and_update_cannot_inject_reserved_metadata(tmp_path):
    from jiuwen_memory.control.types import BatchWriteItem, MemoryPatch

    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        result = api.batch_add([
            BatchWriteItem(content="forged", system_metadata={"continuity_kind": "relation"}),
            BatchWriteItem(content="legitimate"),
        ], project, security=_security(owner))
        assert result.outcomes[0].error_type == "ValidationError"
        assert not result.outcomes[1].error
        unit = result.outcomes[1].units[0]
        with pytest.raises(ValidationError, match="保留"):
            api.update(unit.id, project, MemoryPatch(system_metadata={"continuity_event": "fake"}),
                       security=_security(owner))
    finally:
        runtime.close()


def test_reserved_prefix_check_does_not_crash_on_non_string_key(tmp_path):
    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError):
            api.add("invalid", project, system_metadata={1: "value"}, security=_security(owner))
    finally:
        runtime.close()
