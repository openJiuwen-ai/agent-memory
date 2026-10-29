"""Full assembly and PEP tests of the S10 public surface."""

import pytest

from jiuwen_memory.api import (
    MemoryRef,
    RelationRecord,
    RoleViewQuery,
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
                    "params": {
                        name: "default"
                        for name in [
                            "ingestor",
                            "retriever",
                            "evolver",
                            "index_builder",
                            "classifier",
                            "scheduler",
                            "policy",
                        ]
                    },
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
                    "params": {"single_writer": True, "allow_ephemeral": True},
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
        task = api.role_task(project, TaskRequest("create", "create"), security=security)
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
        result = api.role_relation(project, relation, 0, security=security)
        assert result["status"] == "committed"
        view = api.role_view(
            project, RoleViewQuery(anchor=MemoryRef.of(a), duty="testing"), security=security
        )
        assert any(p["steps"][-1]["relation_id"] == "r" for p in view["paths"])
    finally:
        runtime.close()


def test_three_principals_resume_without_handoff_and_source_can_be_isolated(tmp_path):
    from dataclasses import asdict
    from pathlib import Path

    import yaml

    from jiuwen_memory.api import ContinuityEvent, PermissionDeniedError, SpaceMember

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
        task = api.role_task(project, TaskRequest("create", "create"), security=security)
        bindings = {}
        for name in actors:
            task = api.role_task(
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
        event = ContinuityEvent(task["task_id"], bindings["workbuddy"], "event", "observed timeout")
        receipt = api.role_record(
            project, event, security=_security(actors["workbuddy"])
        )
        assert receipt["status"] == "committed"
        for name in ("hermes", "jiuwenswarm"):
            view = api.role_view(
                project,
                RoleViewQuery(task_id=task["task_id"], duty="diagnosis"),
                security=_security(actors[name]),
            )
            assert any(item["text"] == "observed timeout" for item in view["facts"])
            assert view["meta"]["based_on_handoff"] is None
        with pytest.raises(PermissionDeniedError):
            api.role_record(project, event, security=_security(actors["hermes"]))
        # A trusted configuration update isolates the source; binding alone cannot bypass it.
        api._continuity_connectors["workbuddy"]["enabled"] = False
        isolated = api.role_view(
            project,
            RoleViewQuery(task_id=task["task_id"]),
            security=_security(actors["hermes"]),
        )
        assert "observed timeout" not in str(isolated)
        ref = MemoryRef.parse(receipt["refs"][0])
        with pytest.raises(PermissionDeniedError):
            api.get(ref.unit_id, ref.scope, security=_security(actors["hermes"]))
        assert asdict(project) == receipt["refs"][0]["scope"]
    finally:
        runtime.close()
