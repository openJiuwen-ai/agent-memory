"""Full assembly and PEP tests of the S10 public surface."""

import pytest

from jiuwen_memory.api import (
    Context,
    ContinuityWrite,
    MemoryRef,
    RelationRecord,
    Scope,
    TaskRequest,
    assemble_runtime,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import SpaceSpec
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _security(actor, role=Role.USER):
    return internal_context(ScopedAuthenticator(actor, role=role))


def test_public_role_view_and_task_registration():
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
            "continuity": {
                "default": {
                    "target": "local",
                    "params": {"single_writer": True, "allow_ephemeral": True, "mode": "role"},
                }
            },
            "role_view": {"default": {"target": "bounded"}},
        }
    )
    runtime = assemble_runtime(config=config)
    api = runtime.api
    project = Scope(org="acme", space="project")
    owner = Scope(org="acme", user="owner")
    security = _security(owner)
    admin = _security(owner, Role.ADMIN)
    try:
        api.create_space(SpaceSpec(org="acme", space="project", owner=owner), security=admin)
        task = api.continuity_task(project, TaskRequest("create", "create"), security=security)
        assert task["revision"] == 1
        a = api.add("payment timeout", project, security=security)[0]
        b = api.add("a test method", project, security=security)[0]
        relation = RelationRecord(
            "r",
            MemoryRef.of(b),
            MemoryRef.of(a),
            "verification_method_for",
            status="confirmed",
            evidence_refs=[MemoryRef.of(b)],
        )
        result = api.continuity_relation(project, relation, 0, security=security)
        assert result["status"] == "committed"
        view = api.search(
            "", Context(project), security=security,
            anchor=MemoryRef.of(a), duty="testing",
        )
        assert any(p["steps"][-1]["relation_id"] == "r" for p in view.paths)
    finally:
        runtime.close()


def test_three_principals_resume_without_handoff_and_source_can_be_isolated(tmp_path):
    from dataclasses import asdict
    from pathlib import Path

    import yaml

    from jiuwen_memory.api import PermissionDeniedError, SpaceMember

    config = yaml.safe_load(Path("examples/role_memory/server.yaml").read_text())
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    runtime = assemble_runtime(config=config)
    api = runtime.api
    project = Scope(org="demo", space="role-project")
    owner = Scope(org="demo", user="owner")
    security = _security(owner)
    try:
        api.create_space(
            SpaceSpec(org="demo", space="role-project", owner=owner),
            security=_security(Scope(org="demo", user="ops"), Role.ADMIN),
        )
        actors = {
            name: Scope(**value["actor"])
            for name, value in config["globals"]["continuity_connectors"].items()
        }
        for actor in actors.values():
            api.add_space_member(
                "demo", "role-project", SpaceMember(scope=actor), security=security
            )
        task = api.continuity_task(project, TaskRequest("create", "create"), security=security)
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
                    context_id="local-session",
                ),
                security=security,
            )
            bindings[name] = task["task"]["bindings"][-1]["binding_id"]
        workbuddy_binding = bindings.get("workbuddy")
        assert workbuddy_binding is not None, "WorkBuddy binding was not registered"
        write = ContinuityWrite(task["task_id"], workbuddy_binding, "event")
        units = api.add(
            "observed timeout", project,
            security=_security(actors["workbuddy"]), continuity=write,
        )
        assert units[0].content == "observed timeout"
        for name in ("hermes", "jiuwenswarm"):
            view = api.search(
                "", Context(project), security=_security(actors[name]),
                task_id=task["task_id"], duty="diagnosis",
            )
            assert any(item.content == "observed timeout" for item in view.items)
            assert view.view_meta["based_on_handoff"] is None
        with pytest.raises(PermissionDeniedError):
            api.add(
                "observed timeout", project,
                security=_security(actors["hermes"]), continuity=write,
            )
        # A trusted configuration update isolates the source; binding alone cannot bypass it.
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable-next=protected-access
        api._continuity_connectors["workbuddy"]["enabled"] = False
        isolated = api.search(
            "", Context(project), security=_security(actors["hermes"]),
            task_id=task["task_id"],
        )
        assert "observed timeout" not in str(isolated.items)
        ref = MemoryRef.of(units[0])
        with pytest.raises(PermissionDeniedError):
            api.get(ref.unit_id, ref.scope, security=_security(actors["hermes"]))
        assert asdict(project) == asdict(units[0].scope)
    finally:
        runtime.close()
