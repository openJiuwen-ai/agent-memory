"""Source governance through the public API: dynamic quarantine blocks reads per range."""

# 白盒夹具：这些用例直接读写被测对象的私有成员来注入故障/断言内部阶段，
# 属于测试专用手法，不在生产代码里开放测试钩子。
# pylint: disable=protected-access

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from jiuwen_memory.api import (
    Context,
    ContinuityWrite,
    MemoryRef,
    PermissionDeniedError,
    RelationRecord,
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
            "demo",
            "role-project",
            SpaceMember(scope=Scope(**value["actor"])),
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
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bindings = {}
        for name in actors:
            task = api.continuity_task(
                project,
                TaskRequest(
                    "bind",
                    name,
                    task["task_id"],
                    task["revision"],
                    connector_id=name,
                    context_id="session",
                ),
                security=_security(owner),
            )
            bindings[name] = task["task"]["bindings"][-1]["binding_id"]
        workbuddy_binding = bindings.get("workbuddy")
        hermes_binding = bindings.get("hermes")
        assert workbuddy_binding is not None, "WorkBuddy binding was not registered"
        assert hermes_binding is not None, "Hermes binding was not registered"
        units = api.add(
            "workbuddy fact",
            project,
            security=_security(actors["workbuddy"]),
            continuity=ContinuityWrite(task["task_id"], workbuddy_binding, "wb-event"),
        )
        api.add(
            "hermes fact",
            project,
            security=_security(actors["hermes"]),
            continuity=ContinuityWrite(task["task_id"], hermes_binding, "h-event"),
        )

        wb_ref = MemoryRef.of(units[0])
        reader = _security(actors["hermes"])
        assert api.get(wb_ref.unit_id, wb_ref.scope, security=reader).content == "workbuddy fact"

        # Dynamic quarantine with reason; only the workbuddy range becomes unavailable.
        api.quarantine_source(project, "workbuddy", "injected bad results", security=_gov(ops))
        with pytest.raises(PermissionDeniedError):
            api.get(wb_ref.unit_id, wb_ref.scope, security=reader)
        view = api.search("", Context(project), security=reader, task_id=task["task_id"])
        assert "workbuddy fact" not in str(view.items)
        # Other sources remain readable in the same project.
        assert "hermes fact" in str(view.items)

        # Release restores the source.
        assert api.release_source(project, "workbuddy", security=_gov(ops)) is True
        assert api.get(wb_ref.unit_id, wb_ref.scope, security=reader).content == "workbuddy fact"
    finally:
        runtime.close()


def test_quarantine_entry_is_governed(tmp_path):
    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    try:
        # A non-governance actor cannot isolate or release a source range.
        with pytest.raises(PermissionDeniedError):
            api.quarantine_source(project, "workbuddy", "reason", security=_security(owner))
        assert api.release_source(project, "workbuddy", security=_gov(ops)) is False
    finally:
        runtime.close()


def test_public_record_derives_and_enforces_source_dependency(tmp_path):
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        actor = Scope(**config["globals"]["continuity_connectors"]["workbuddy"]["actor"])
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest(
                "bind", "bind", task["task_id"], 1, connector_id="workbuddy", context_id="session"
            ),
            security=_security(owner),
        )
        extractor = Mock()
        extractor.extract.return_value = [
            MemoryUnit(id="proposed", scope=project, segments=[Segment(content="derived fact")])
        ]
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity.derivation_extractor = extractor
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity.derivation_version = "test/1"
        write = ContinuityWrite(
            task["task_id"], bound["task"]["bindings"][0]["binding_id"], "event"
        )
        first = api.add("source event", project, security=_security(actor), continuity=write)
        output = MemoryRef.of(first[0])
        derived = api.get(output.unit_id, output.scope, security=_security(actor))
        assert derived.content == "source event"
        replay = api.add("source event", project, security=_security(actor), continuity=write)
        assert [u.id for u in replay] == [u.id for u in first]
        extractor.extract.assert_called_once()
        api.quarantine_source(project, "workbuddy", "test quarantine", security=_gov(ops))
        with pytest.raises(PermissionDeniedError):
            api.get(output.unit_id, output.scope, security=_security(actor))
    finally:
        runtime.close()


def test_cross_space_source_quarantine_uses_trusted_recall_origin(tmp_path, monkeypatch):
    from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem

    runtime, api, project, owner, ops, _ = _runtime(tmp_path)
    other = Scope(org=project.org, space="other-project")
    try:
        api.create_space(
            SpaceSpec(org=other.org, space=other.space, owner=owner),
            security=_gov(ops),
        )
        api.quarantine_source(project, "workbuddy", "invalid source", security=_gov(ops))

        async def recall(scope, query):
            return RetrievalResult(
                items=[
                    RetrievedItem(
                        unit_id=scope.space,
                        content=scope.space,
                        system_metadata={"continuity_connector": "workbuddy", "space": other.space},
                    )
                ]
            )

        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        monkeypatch.setattr(api._queries, "recall", recall)
        result = api.search(
            "evidence",
            Context(
                Scope(org=project.org),
                extensions={
                    "spaces": [project.space, other.space],
                },
            ),
            security=_security(owner),
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
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        task = api.continuity_task(
            project,
            TaskRequest(
                "bind",
                "bind",
                task["task_id"],
                task["revision"],
                connector_id=connector,
                context_id="time-test",
            ),
            security=_security(owner),
        )
        write = ContinuityWrite(task["task_id"], task["task"]["bindings"][0]["binding_id"], "event")
        units = api.add(
            "old observation",
            project,
            security=_security(actor),
            continuity=write,
            event_time=datetime.fromisoformat("2024-02-03T12:00:00+08:00"),
        )
        unit = api.get(units[0].id, project, security=_security(actor))
        assert unit.temporal.t_event == datetime.fromisoformat("2024-02-03T12:00:00+08:00")
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "metadata",
    [
        {"continuity_envelope": '{"occurred_at":"2001-01-01T00:00:00Z"}'},
        {"continuity_envelope": "not json"},
        {"continuity_envelope": '{"occurred_at":1}'},
        {"continuity_envelope": '{"occurred_at":"2001-01-01T00:00:00"}'},
        {"continuity_connector": "workbuddy"},
        {"continuity_kind": "relation"},
        {"continuity_event": "forged"},
        {"continuity_future_reserved": "value"},
    ],
)
@pytest.mark.parametrize("method", ["add", "batch_add"])
def test_public_writes_reject_continuity_metadata(tmp_path, metadata, method):
    from jiuwen_memory.control.types import BatchWriteItem

    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError, match="保留"):
            if method == "add":
                api.add("forged", project, system_metadata=metadata, security=_security(owner))
            else:
                api.batch_add(
                    [BatchWriteItem(content="forged")],
                    project,
                    system_metadata=metadata,
                    security=_security(owner),
                )
    finally:
        runtime.close()


def test_explicit_event_time_is_independent_of_message_time(tmp_path):
    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        event = datetime(2001, 1, 1, tzinfo=UTC)
        message = datetime(2024, 2, 3, tzinfo=UTC)
        unit = api.add(
            "observation", project, event_time=event, occurred_at=message, security=_security(owner)
        )[0]
        assert unit.temporal.t_event == event
        assert unit.temporal.t_message == message
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
        result = api.batch_add(
            [
                BatchWriteItem(content="forged", system_metadata={"continuity_kind": "relation"}),
                BatchWriteItem(content="legitimate"),
            ],
            project,
            security=_security(owner),
        )
        assert result.outcomes[0].error_type == "ValidationError"
        assert not result.outcomes[1].error
        unit = result.outcomes[1].units[0]
        with pytest.raises(ValidationError, match="保留"):
            api.update(
                unit.id,
                project,
                MemoryPatch(system_metadata={"continuity_event": "fake"}),
                security=_security(owner),
            )
    finally:
        runtime.close()


def test_reserved_prefix_check_does_not_crash_on_non_string_key(tmp_path):
    runtime, api, project, owner, _, _ = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError):
            api.add("invalid", project, system_metadata={1: "value"}, security=_security(owner))
    finally:
        runtime.close()


def test_add_with_continuity_is_idempotent_and_returns_units(tmp_path):

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest(
                "bind",
                "workbuddy",
                task["task_id"],
                task["revision"],
                connector_id="workbuddy",
                context_id="session",
            ),
            security=_security(owner),
        )
        binding_id = bound["task"]["bindings"][-1]["binding_id"]
        continuity = ContinuityWrite(
            task["task_id"],
            binding_id,
            "wb-evt-1",
            kind="result",
            source_locator="jira:WB-101",
        )
        writer = _security(actors["workbuddy"])
        units = api.add(
            "v2 下批量 20 复测通过",
            project,
            security=writer,
            continuity=continuity,
            tags=["duty:testing"],
        )
        assert [u.content for u in units] == ["v2 下批量 20 复测通过"]
        assert units[0].system_metadata["continuity_event"] == "wb-evt-1"
        assert "duty:testing" in (units[0].tags or [])
        # source_locator lands on MemoryUnit.source_ref (S11 5.2 落盘映射).
        assert units[0].source_ref == "jira:WB-101"
        replay = api.add(
            "v2 下批量 20 复测通过",
            project,
            security=writer,
            continuity=continuity,
            tags=["duty:testing"],
        )
        assert [u.id for u in replay] == [u.id for u in units]
        # 改了会改变落盘内容的参数就不是同一请求：必须冲突，不能复用旧回执。
        with pytest.raises(Exception, match="different content"):
            api.add(
                "v2 下批量 20 复测通过",
                project,
                security=writer,
                continuity=continuity,
                tags=["duty:testing"],
                event_time=datetime.fromisoformat("2025-01-01T00:00:00+08:00"),
            )
    finally:
        runtime.close()


def test_add_with_continuity_raises_write_incomplete_on_partial_receipt(tmp_path, monkeypatch):
    from jiuwen_memory.api import WriteIncompleteError

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest(
                "bind",
                "workbuddy",
                task["task_id"],
                task["revision"],
                connector_id="workbuddy",
                context_id="session",
            ),
            security=_security(owner),
        )
        binding_id = bound["task"]["bindings"][-1]["binding_id"]
        continuity = ContinuityWrite(task["task_id"], binding_id, "wb-evt-2")
        original = api._role_writer

        def failing_writer(scope, security, **_kwargs):
            def write(content, metadata):
                raise OSError("content store unavailable")

            return write

        monkeypatch.setattr(api, "_role_writer", failing_writer)
        with pytest.raises(WriteIncompleteError) as raised:
            api.add("lost", project, security=_security(actors["workbuddy"]), continuity=continuity)
        assert raised.value.operation_id
        assert raised.value.status in {"unknown", "partial"}
        assert raised.value.retry_action in {"retry_original_write", "recover_then_retry"}
        monkeypatch.setattr(api, "_role_writer", original)
    finally:
        runtime.close()


def test_batch_add_with_continuity_is_per_item_idempotent(tmp_path):
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest(
                "bind",
                "workbuddy",
                task["task_id"],
                task["revision"],
                connector_id="workbuddy",
                context_id="session",
            ),
            security=_security(owner),
        )
        binding_id = bound["task"]["bindings"][-1]["binding_id"]
        writer = _security(actors["workbuddy"])
        first = api.batch_add(
            [
                BatchWriteItem(
                    "history 1",
                    project,
                    continuity=ContinuityWrite(task["task_id"], binding_id, "h-1"),
                ),
                BatchWriteItem("plain record", project),
                BatchWriteItem(
                    "history 2",
                    project,
                    continuity=ContinuityWrite(task["task_id"], binding_id, "h-2"),
                ),
            ],
            project,
            security=writer,
        )
        by_index = {outcome.index: outcome for outcome in first.outcomes}
        assert by_index[0].continuity_status == "committed"
        assert by_index[0].operation_id
        assert by_index[1].continuity_status == ""
        assert by_index[2].continuity_status == "committed"
        # Replaying the same events does not duplicate the saved units.
        replay = api.batch_add(
            [
                BatchWriteItem(
                    "history 1",
                    project,
                    continuity=ContinuityWrite(task["task_id"], binding_id, "h-1"),
                ),
                BatchWriteItem(
                    "history 2",
                    project,
                    continuity=ContinuityWrite(task["task_id"], binding_id, "h-2"),
                ),
            ],
            project,
            security=writer,
        )
        assert [outcome.units[0].id for outcome in replay.outcomes] == [
            by_index[0].units[0].id,
            by_index[2].units[0].id,
        ]
    finally:
        runtime.close()


def test_search_with_duty_returns_view_paths_and_meta(tmp_path):

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        api.continuity_task(project, TaskRequest("create", "create"), security=_security(owner))
        a = api.add("payment timeout", project, security=_security(owner))[0]
        b = api.add("a test method", project, security=_security(owner))[0]
        api.continuity_relation(
            project,
            RelationRecord(
                "r",
                MemoryRef.of(b),
                MemoryRef.of(a),
                "verification_method_for",
                status="confirmed",
                evidence_refs=[MemoryRef.of(b)],
            ),
            0,
            security=_security(owner),
        )
        result = api.search(
            "",
            Context(project),
            security=_security(owner),
            anchor=MemoryRef.of(a),
            duty="testing",
            top_k=5,
        )
        assert result.paths, "职责视图应返回路径"
        assert result.view_meta is not None
        assert result.view_meta["duties"] == ["testing"]
        assert any(item.view for item in result.items)
    finally:
        runtime.close()


def test_search_view_is_rejected_with_cross_space(tmp_path):

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        with pytest.raises(ValidationError):
            api.search(
                "q",
                Context(project, {"spaces": []}),
                security=_security(owner),
                duty="testing",
            )
    finally:
        runtime.close()


def test_list_filters_quarantined_source_without_failing_the_page(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bindings = {}
        for name in ("workbuddy", "hermes"):
            task = api.continuity_task(
                project,
                TaskRequest(
                    "bind",
                    name,
                    task["task_id"],
                    task["revision"],
                    connector_id=name,
                    context_id="session",
                ),
                security=_security(owner),
            )
            bindings[name] = task["task"]["bindings"][-1]["binding_id"]
        api.add(
            "workbuddy fact",
            project,
            security=_security(actors["workbuddy"]),
            continuity=ContinuityWrite(
                task["task_id"], bindings.get("workbuddy", ""), "wb-1"
            ),
        )
        api.add(
            "hermes fact",
            project,
            security=_security(actors["hermes"]),
            continuity=ContinuityWrite(
                task["task_id"], bindings.get("hermes", ""), "h-1"
            ),
        )

        api.quarantine_source(project, "workbuddy", "injected bad results", security=_gov(ops))

        # One quarantined source no longer fails the whole page: it is filtered before
        # pagination, and count reflects the filtered page.
        page = api.list(project, security=_security(actors["hermes"]))
        contents = [unit.content for unit in page.items]
        assert "hermes fact" in contents
        assert "workbuddy fact" not in contents
        assert page.count == len(contents)

        api.release_source(project, "workbuddy", security=_gov(ops))
        restored = api.list(project, security=_security(actors["hermes"]))
        assert "workbuddy fact" in [unit.content for unit in restored.items]
    finally:
        runtime.close()


def test_list_drops_entries_whose_dependency_evidence_changed(tmp_path, monkeypatch):
    """`list` must not hand back a conclusion `get` already refuses to read."""
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])
        evidence = api.add(
            "evidence", project, security=wb,
            continuity=ContinuityWrite(task["task_id"], binding, "e-1"),
        )[0]
        conclusion = api.add(
            "conclusion", project, security=wb,
            continuity=ContinuityWrite(
                task["task_id"], binding, "e-2",
                evidence_refs=[MemoryRef.of(evidence)],
            ),
        )[0]
        assert "conclusion" in [u.content for u in api.list(project, security=wb).items]

        # Revise the evidence: its digest changes, so the conclusion's dependency is stale.
        from jiuwen_memory.api import MemoryPatch

        api.update(
            evidence.id, project, MemoryPatch(content="evidence v2"), security=wb,
        )
        with pytest.raises(Exception):
            api.get(conclusion.id, project, security=wb)
        assert "conclusion" not in [u.content for u in api.list(project, security=wb).items]
    finally:
        runtime.close()


def test_view_path_honours_caller_filters(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        api.continuity_task(project, TaskRequest("create", "create"), security=_security(owner))
        anchor = api.add(
            "payment timeout", project, security=_security(owner), tags=["billing"],
        )[0]
        result = api.search(
            "", Context(project), security=_security(owner),
            anchor=MemoryRef.of(anchor), duty="testing",
            filters={"tags": {"eq": "security"}},
        )
        ids = [item.unit_id for item in result.items]
        assert anchor.id not in ids, "filtered-out anchor must not leak through the view path"
    finally:
        runtime.close()


def test_add_with_continuity_forwards_user_metadata_and_rejects_infer(tmp_path):
    from jiuwen_memory.api import ValidationError as ApiValidationError

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])
        units = api.add(
            "with metadata", project, security=wb,
            continuity=ContinuityWrite(task["task_id"], binding, "m-1"),
            user_metadata={"origin": "ticket"}, tags=["duty:testing"],
        )
        assert units[0].user_metadata["origin"] == "ticket"
        assert units[0].tags and "duty:testing" in units[0].tags
        with pytest.raises(ApiValidationError):
            api.add(
                "bad", project, security=wb,
                continuity=ContinuityWrite(task["task_id"], binding, "m-2"),
                system_metadata={"infer": "true"},
            )
    finally:
        runtime.close()


def _continuity_only_runtime(tmp_path):
    """Minimal continuity-mode runtime: no graph store / role_view / relation keys."""
    from jiuwen_memory.config import Config

    config = Config.from_dict(
        {
            "engine": {
                "default": {
                    "target": "cloud",
                    "params": dict.fromkeys(
                        ("ingestor", "retriever", "evolver", "index_builder",
                         "classifier", "scheduler", "policy"),
                        "default",
                    ),
                }
            },
            "authorizer": {
                "default": {"target": "space_aware", "params": {"delegate": "standard"}},
                "standard": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                },
            },
            "kv_store": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "mem.db"), "fixed_path": True},
                },
                "registry": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "registry.db"), "fixed_path": True},
                },
            },
            "continuity": {
                "default": {
                    "target": "local",
                    "params": {
                        "mode": "continuity",
                        "single_writer": True,
                        "registry_store": "registry",
                    },
                }
            },
            "globals": {
                "continuity_connectors": {
                    "workbuddy": {
                        "actor": {
                            "org": "demo", "space": "", "user": "workbuddy",
                            "agent": "", "session": "",
                        },
                        "projects": ["role-project"],
                        "enabled": True,
                    }
                }
            },
            "permission": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "permissions.db")},
                }
            },
        }
    )
    return assemble_runtime(config=config)


def test_continuity_mode_search_restores_task_background(tmp_path):
    runtime = _continuity_only_runtime(tmp_path)
    api = runtime.api
    project, owner = Scope(org="demo", space="role-project"), Scope(org="demo", user="owner")
    wb = Scope(org="demo", user="workbuddy")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="role-project", owner=owner),
            security=_security(owner, Role.ADMIN),
        )
        api.add_space_member(
            "demo", "role-project", SpaceMember(scope=wb), security=_security(owner),
        )
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        api.add(
            "background fact", project, security=_security(wb),
            continuity=ContinuityWrite(task["task_id"], binding, "bg-1"),
        )
        result = api.search(
            "", Context(project), security=_security(owner), task_id=task["task_id"],
        )
        assert "background fact" in [item.content for item in result.items]
        assert result.view_meta["mode"] == "continuity"
        # 只给 task_id、不要求职责时不需要关系扩展，因此不应声明该缺口。
        assert not any(g["code"] == "relation_expansion_disabled" for g in result.gaps)
        styled = api.search(
            "", Context(project), security=_security(owner),
            task_id=task["task_id"], duty="diagnosis",
        )
        assert any(g["code"] == "relation_expansion_disabled" for g in styled.gaps)
    finally:
        runtime.close()


def test_batch_continuity_stops_after_failure_when_continue_on_error_is_false(tmp_path):
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])
        result = api.batch_add(
            [
                BatchWriteItem("bad", project,
                               continuity=ContinuityWrite(task["task_id"], "missing", "b-1")),
                BatchWriteItem("good", project,
                               continuity=ContinuityWrite(task["task_id"], binding, "b-2")),
            ],
            project, security=wb, continue_on_error=False,
        )
        by_index = {outcome.index: outcome for outcome in result.outcomes}
        assert by_index[0].error_type
        assert by_index[1].error_type == "Skipped"
        assert by_index[1].units == []
        assert "good" not in [u.content for u in api.list(project, security=wb).items]
    finally:
        runtime.close()


def test_batch_mixes_ordinary_and_continuity_in_input_order(tmp_path, monkeypatch):
    """Side effects follow input order across the two batch paths."""
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])

        trace: list[str] = []
        inside_continuity = [False]
        original_aligned = api._commands.batch_write_aligned
        original_batch = api._continuity_batch

        async def spy_aligned(*args, **kwargs):
            # 接续项的正文批写走同一命令端口，不算一次独立的普通项批次。
            if not inside_continuity[0]:
                trace.append("ordinary")
            return await original_aligned(*args, **kwargs)

        def spy_batch(entries, **kwargs):
            trace.append("continuity")
            inside_continuity[0] = True
            try:
                return original_batch(entries, **kwargs)
            finally:
                inside_continuity[0] = False

        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._commands.batch_write_aligned = spy_aligned
        api._continuity_batch = spy_batch
        try:
            api.batch_add(
                [
                    BatchWriteItem("first ordinary", project),
                    BatchWriteItem(
                        "then continuity", project,
                        continuity=ContinuityWrite(task["task_id"], binding, "order-1"),
                    ),
                    BatchWriteItem("last ordinary", project),
                ],
                project, security=wb,
            )
        finally:
            api._commands.batch_write_aligned = original_aligned
            api._continuity_batch = original_batch
        # 顺序是被测性质：普通项先落、接续批随后、最后一个普通项最后。接续批的正文写入
        # 复用同一命令端口，会在其后追加 ordinary 记录，故只断言前三个与批次数。
        assert trace[:3] == ["ordinary", "continuity", "ordinary"], trace
        assert trace.count("continuity") == 1, trace
    finally:
        runtime.close()


def test_batch_keeps_every_index_when_a_middle_item_fails(tmp_path):
    """A mid-batch failure must still return one outcome per input index."""
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])
        result = api.batch_add(
            [
                BatchWriteItem("ok ordinary", project),
                BatchWriteItem("bad ordinary", project,
                               system_metadata={"continuity_event": "fake"}),
                BatchWriteItem(
                    "later continuity", project,
                    continuity=ContinuityWrite(task["task_id"], binding, "order-3"),
                ),
            ],
            project, security=wb, continue_on_error=False,
        )
        by_index = {outcome.index: outcome for outcome in result.outcomes}
        assert sorted(by_index) == [0, 1, 2]
        assert not by_index[0].error
        assert by_index[1].error_type == "ValidationError"
        assert by_index[2].error_type == "Skipped"
        assert "ok ordinary" in [
            unit.content for unit in api.list(project, security=wb).items
        ]
        assert "later continuity" not in [
            unit.content for unit in api.list(project, security=wb).items
        ]
    finally:
        runtime.close()


def test_relation_revision_requires_submitter_or_governance(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        a = api.add("payment timeout", project, security=_security(owner))[0]
        b = api.add("a test method", project, security=_security(owner))[0]
        relation = RelationRecord(
            "r", MemoryRef.of(b), MemoryRef.of(a), "verification_method_for",
            status="confirmed", evidence_refs=[MemoryRef.of(b)],
        )
        api.continuity_relation(project, relation, 0, security=_security(owner))
        head = api._role_service(project).relation_head(project, "r")
        assert head.get("connector") == "", "project-owner submissions carry no connector"
        # A legacy head without a connector is governance-only, even for the endpoints owner.
        revised = RelationRecord(
            "r", MemoryRef.of(b), MemoryRef.of(a), "verification_method_for",
            status="confirmed", evidence_refs=[MemoryRef.of(b)], revision=2,
        )
        with pytest.raises(PermissionDeniedError):
            api.continuity_relation(project, revised, 1, security=_security(actors["workbuddy"]))
    finally:
        runtime.close()


def test_relation_with_unavailable_connector_is_unresolvable(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        a = api.add("cause", project, security=_security(owner))[0]
        b = api.add("effect", project, security=_security(owner))[0]
        relation = RelationRecord(
            "r2", MemoryRef.of(b), MemoryRef.of(a), "supports",
            status="confirmed", evidence_refs=[MemoryRef.of(b)],
        )
        api.continuity_relation(project, relation, 0, security=_security(owner))
        service = api._role_service(project)
        head = service.relation_head(project, "r2")
        head["connector"] = "workbuddy"
        service.repository.save(project, "relations", "r2", head)
        assert service.resolve_edge(project, "r2", lambda ref: None).relation_id == "r2"

        # Disabling the submitting connector makes the relation unusable, indistinguishably
        # from a missing relation (D3). The gate reads the injected connector config.
        service.connectors["workbuddy"]["enabled"] = False
        with pytest.raises(Exception):
            service.resolve_edge(project, "r2", lambda ref: None)
        coverage = api.continuity_recover(project, security=_security(owner))
        assert "reference_unavailable" in (coverage.get("gaps") or [])
    finally:
        runtime.close()


def test_continuity_batch_writes_bodies_and_index_in_one_call(tmp_path):
    """性能护栏：一次 batch_add 里的接续项只触发一次正文批写与一次索引构建。"""
    from jiuwen_memory.api import BatchWriteItem
    from jiuwen_memory.control.types import BatchWriteOutcome, BatchWriteResult

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])

        body_calls: list[int] = []
        index_calls: list[int] = []
        original_write = api._commands.batch_write
        builder = api._queries._engine._index
        original_build = builder.build

        async def counting_write(items, **kwargs):
            body_calls.append(len(items))
            return await original_write(items, **kwargs)

        def counting_build(units, **kwargs):
            index_calls.append(len(units))
            return original_build(units, **kwargs)

        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._commands.batch_write = counting_write
        builder.build = counting_build
        try:
            result = api.batch_add(
                [
                    BatchWriteItem(
                        f"body {index}", project,
                        continuity=ContinuityWrite(task["task_id"], binding, f"perf-{index}"),
                    )
                    for index in range(4)
                ],
                project, security=wb,
            )
        finally:
            api._commands.batch_write = original_write
            builder.build = original_build
        assert all(outcome.continuity_status == "committed" for outcome in result.outcomes)
        # 正文只经命令端口调用一次，且这一次携带全部条目（不退回逐条 write）。
        assert body_calls == [4], f"expected one batched body write, got {body_calls}"
        # 引擎按条目建索引（既有行为），护栏是「每个条目恰好一次、没有重复建」。
        assert sum(index_calls) == 4 and len(index_calls) <= 4, index_calls
        assert isinstance(result, BatchWriteResult) and all(
            isinstance(outcome, BatchWriteOutcome) for outcome in result.outcomes
        )
    finally:
        runtime.close()


def test_batch_and_single_continuity_writes_match_on_marks_source_ref_and_audit(tmp_path):
    """批量导入与单条 add 写出的条目必须在作者标记、source_ref、审计上完全一致。"""
    from jiuwen_memory.api import BatchWriteItem
    from jiuwen_memory.common.security import principal

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])

        single = api.add(
            "single body", project, security=wb,
            continuity=ContinuityWrite(
                task["task_id"], binding, "parity-1", source_locator="jira:PARITY-1"
            ),
        )[0]
        batched = api.batch_add(
            [
                BatchWriteItem(
                    "batch body", project,
                    continuity=ContinuityWrite(
                        task["task_id"], binding, "parity-2", source_locator="jira:PARITY-2"
                    ),
                )
            ],
            project, security=wb,
        ).outcomes[0].units[0]

        for unit in (single, batched):
            assert principal.AUTHOR_PRINCIPAL in unit.system_metadata, unit.system_metadata
            assert principal.AUTHOR_AGENT in unit.system_metadata, unit.system_metadata
        assert single.system_metadata[principal.AUTHOR_PRINCIPAL] == (
            batched.system_metadata[principal.AUTHOR_PRINCIPAL]
        )
        # source_locator → MemoryUnit.source_ref 在两条路径上都生效。
        assert single.source_ref == "jira:PARITY-1"
        assert batched.source_ref == "jira:PARITY-2"
        # 审计：两条路径都留下 add 记录。
        events = api.audit({}, security=_security(owner, Role.ROOT), limit=200)
        actions = [getattr(entry, "action", "") for entry in events]
        assert actions.count("add") >= 2, actions
    finally:
        runtime.close()


def test_derived_output_does_not_inherit_source_event_time(tmp_path):
    """F10 4.2：派生输出是按构建时刻生成的新记忆，不继承来源的发生时间。"""
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def import MemoryUnit, Segment

    marker = datetime.fromisoformat("2001-01-01T00:00:00+08:00")
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        actor = Scope(**config["globals"]["continuity_connectors"]["workbuddy"]["actor"])
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "bind", task["task_id"], 1, connector_id="workbuddy",
                        context_id="session"),
            security=_security(owner),
        )
        extractor = Mock()
        extractor.extract.return_value = [
            MemoryUnit(id="proposed", scope=project, segments=[Segment(content="derived fact")])
        ]
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity.derivation_extractor = extractor
        # pylint: disable-next=protected-access
        api._continuity.derivation_version = "test/1"
        source = api.add(
            "source event", project, security=_security(actor),
            continuity=ContinuityWrite(
                task["task_id"], bound["task"]["bindings"][0]["binding_id"], "event"
            ),
            event_time=marker,
        )[0]
        assert source.temporal.t_event == marker, "原始事件必须继承事件时间"
        derived_units = [
            unit
            for unit in api.list(project, security=_security(actor), limit=50).items
            if unit.system_metadata.get("continuity_output")
        ]
        assert derived_units, "派生输出应当存在"
        assert all(
            unit.temporal.t_event is None for unit in derived_units
        ), [(unit.content, unit.temporal.t_event) for unit in derived_units]
    finally:
        runtime.close()


def test_continuity_view_reports_truncation_gap(tmp_path):
    runtime = _continuity_only_runtime(tmp_path)
    api = runtime.api
    project, owner = Scope(org="demo", space="role-project"), Scope(org="demo", user="owner")
    wb = Scope(org="demo", user="workbuddy")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="role-project", owner=owner),
            security=_security(owner, Role.ADMIN),
        )
        api.add_space_member("demo", "role-project", SpaceMember(scope=wb),
                             security=_security(owner))
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        for index in range(2):
            api.add(
                f"background {index}", project, security=_security(wb),
                continuity=ContinuityWrite(task["task_id"], binding, f"bg-{index}"),
            )
        result = api.search(
            "", Context(project), security=_security(owner),
            task_id=task["task_id"], top_k=1,
        )
        assert len(result.items) == 1
        # 背景排在语义候选之前：截断保留的是背景，不是候选。
        assert result.items[0].view["discovery"] == "background"
        assert any(g["code"] == "budget_exhausted" for g in result.gaps), result.gaps
    finally:
        runtime.close()


def test_batch_continuity_uses_resolved_scope_when_items_carry_none(tmp_path):
    """调用方只给批次 Scope 时，回执仍须用解析后的落点，不能在写入后崩在 None 上。"""
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="s"),
            security=_security(owner),
        )
        binding = bound["task"]["bindings"][-1]["binding_id"]
        wb = _security(actors["workbuddy"])
        result = api.batch_add(
            [
                BatchWriteItem("no scope 1", continuity=ContinuityWrite(
                    task["task_id"], binding, "ns-1")),
                BatchWriteItem("no scope 2", continuity=ContinuityWrite(
                    task["task_id"], binding, "ns-2")),
            ],
            project, security=wb,
        )
        assert [outcome.continuity_status for outcome in result.outcomes] == [
            "committed", "committed"
        ]
        assert all(outcome.operation_id for outcome in result.outcomes)
        assert all(outcome.units for outcome in result.outcomes)
        assert sorted(
            unit.content for unit in api.list(project, security=wb).items
        ) == ["no scope 1", "no scope 2"]
    finally:
        runtime.close()


_S11_ANNOTATION_KEYS = {
    "kind", "status", "discovery", "required", "classification_confidence",
    "evidence_refs", "conditions", "duty_ids", "valid_duties", "common", "path_ids",
}
_S11_KINDS = {
    "observation", "statement", "inference", "suggestion", "decision",
    "experience", "open_issue", "risk",
}


def test_role_view_annotations_follow_s11(tmp_path):
    """D6/S11：角色档报 mode 与版本；标注是完整 S11 字段，取构建器算出的真实值。"""
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        problem = api.add("payment timeout", project, security=_security(owner))[0]
        method = api.add("replay the batch with size 20", project, security=_security(owner))[0]
        api.continuity_relation(
            project,
            RelationRecord(
                "r-test", MemoryRef.of(method), MemoryRef.of(problem),
                "verification_method_for", status="confirmed",
                evidence_refs=[MemoryRef.of(method)],
            ),
            0,
            security=_security(owner),
        )
        result = api.search(
            "", Context(project), security=_security(owner),
            anchor=MemoryRef.of(problem), duty="testing",
        )
        assert result.view_meta["mode"] == "role"
        assert result.view_meta["schema_version"] == "role-view/2"
        by_id = {item.unit_id: item.view for item in result.items}
        for view in by_id.values():
            assert set(view) == _S11_ANNOTATION_KEYS, view
            assert view["kind"] in _S11_KINDS, view
        assert by_id[problem.id]["discovery"] == "anchor"
        assert by_id[problem.id]["required"] is True
        assert by_id[method.id]["discovery"] == "relation"
        assert by_id[method.id]["required"] is False, "relation endpoints are not required premises"
        path_ids = {path["path_id"] for path in result.paths}
        assert by_id[method.id]["path_ids"], "a relation item must point at its path"
        assert set(by_id[method.id]["path_ids"]) <= path_ids, (by_id[method.id], path_ids)
        assert [item.unit_id for item in result.items][0] == problem.id, "anchor sorts first"
    finally:
        runtime.close()


def test_continuity_anchor_unreadable_reports_gap(tmp_path):
    """读不到 anchor 时要报缺口：否则调用方分不清「没有」和「读不到」。"""
    from jiuwen_memory.api import MemoryRef as Ref

    runtime = _continuity_only_runtime(tmp_path)
    api = runtime.api
    project, owner = Scope(org="demo", space="role-project"), Scope(org="demo", user="owner")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="role-project", owner=owner),
            security=_security(owner, Role.ADMIN),
        )
        missing = Ref(scope=project, unit_id="does-not-exist", content_digest="0" * 64)
        result = api.search(
            "", Context(project), security=_security(owner), anchor=missing,
        )
        assert any(g["code"] == "reference_unavailable" for g in result.gaps), result.gaps
    finally:
        runtime.close()


def test_general_duty_list_does_not_report_expansion_gap(tmp_path):
    runtime = _continuity_only_runtime(tmp_path)
    api = runtime.api
    project, owner = Scope(org="demo", space="role-project"), Scope(org="demo", user="owner")
    try:
        api.create_space(
            SpaceSpec(org="demo", space="role-project", owner=owner),
            security=_security(owner, Role.ADMIN),
        )
        task = api.continuity_task(
            project, TaskRequest("create", "create"), security=_security(owner)
        )
        result = api.search(
            "", Context(project), security=_security(owner),
            task_id=task["task_id"], duty=["general"],
        )
        assert not any(
            g["code"] == "relation_expansion_disabled" for g in result.gaps
        ), result.gaps
    finally:
        runtime.close()


def test_public_batch_item_cannot_forge_source_ref(tmp_path):
    from jiuwen_memory.api import BatchWriteItem

    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    try:
        result = api.batch_add(
            [BatchWriteItem("forged", project, source_ref="jira:FAKE")],
            project, security=_security(owner),
        )
        outcome = result.outcomes[0]
        assert outcome.error_type == "ValidationError"
        assert "source_ref" in outcome.error
        assert [unit.content for unit in api.list(project, security=_security(owner)).items][
            :1
        ] == [] or "forged" not in [
            unit.content for unit in api.list(project, security=_security(owner)).items
        ]
    finally:
        runtime.close()
