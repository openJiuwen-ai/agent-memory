"""D13 task-authorization policies: connection self-binding and space administration.

Both policies default to off; the local service template turns them on. The tests
cover the off state (existing deployments unchanged), the on state, and the WRITE
boundary that neither policy relaxes.
"""

import pytest

from jiuwen_memory.api import ContinuityWrite, Scope, TaskRequest, assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import SpaceMember, SpaceSpec
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

PROJECT = Scope(org="acme", space="project")
OWNER = Scope(org="acme", user="owner")
SWARM = Scope(org="acme", user="swarm")
HERMES = Scope(org="acme", user="hermes")
ADMIN = Scope(org="acme", user="space-admin")

CONNECTORS = {
    "jiuwenswarm": {
        "actor": {"org": "acme", "space": "", "user": "swarm", "agent": "", "session": ""},
        "projects": ["project"],
        "enabled": True,
    },
    "hermes": {
        "actor": {"org": "acme", "space": "", "user": "hermes", "agent": "", "session": ""},
        "projects": ["project"],
        "enabled": True,
    },
}


def _security(actor, role=Role.USER):
    return internal_context(ScopedAuthenticator(actor, role=role))


def _config(*, self_bind=False, admin_manage_tasks=False):
    return Config.from_dict(
        {
            "engine": {
                "default": {
                    "target": "cloud",
                    "params": dict.fromkeys(
                        (
                            "ingestor",
                            "retriever",
                            "evolver",
                            "index_builder",
                            "classifier",
                            "scheduler",
                            "policy",
                        ),
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
                    "params": {
                        "single_writer": True,
                        "allow_ephemeral": True,
                        "mode": "continuity",
                        "self_bind": self_bind,
                        "admin_manage_tasks": admin_manage_tasks,
                    },
                }
            },
            "globals": {"continuity_connectors": CONNECTORS},
        }
    )


@pytest.fixture
def runtime_factory():
    runtimes = []

    def _make(**params):
        runtime = assemble_runtime(config=_config(**params))
        runtimes.append(runtime)
        return runtime

    yield _make
    for runtime in runtimes:
        runtime.close()


def _bootstrap(runtime):
    api = runtime.api
    api.create_space(
        SpaceSpec(org="acme", space="project", owner=OWNER),
        security=_security(Scope(org="acme", user="ops"), Role.ADMIN),
    )
    return api


def _member(
    api,
    scope,
    *,
    content=SpaceContentRole.CONTRIBUTOR,
    governance=SpaceGovernanceRole.NONE,
):
    api.add_space_member(
        "acme",
        "project",
        SpaceMember(scope=scope, content_role=content, governance_role=governance),
        security=_security(OWNER),
    )


def _task(api, security, *, operation="create", request_id="t-create"):
    return api.continuity_task(PROJECT, TaskRequest(operation, request_id), security=security)


def _bind(api, security, task, connector_id, *, request_id="bind", context_id="s1"):
    return api.continuity_task(
        PROJECT,
        TaskRequest(
            "bind",
            request_id,
            task["task_id"],
            task["revision"],
            connector_id=connector_id,
            context_id=context_id,
        ),
        security=security,
    )


def test_default_policy_rejects_non_owner_bind(runtime_factory):
    api = _bootstrap(runtime_factory())
    _member(api, SWARM)
    task = _task(api, _security(OWNER))
    with pytest.raises(PermissionDeniedError):
        _bind(api, _security(SWARM), task, "jiuwenswarm")


def test_self_bind_allows_own_connector(runtime_factory):
    api = _bootstrap(runtime_factory(self_bind=True))
    _member(api, SWARM)
    read_only = Scope(org="acme", user="read-only")
    _member(api, read_only, content=SpaceContentRole.VIEWER)
    task = _task(api, _security(OWNER))
    bound = _bind(api, _security(SWARM), task, "jiuwenswarm")
    assert bound["revision"] == task["revision"] + 1, "own connector self-bind not applied"
    assert any(
        b["connector_id"] == "jiuwenswarm" and b["active"]
        for b in bound["task"]["bindings"]
    ), "binding missing"
    # Self-binding never replaces the project WRITE requirement.
    with pytest.raises(PermissionDeniedError):
        _bind(api, _security(read_only), bound, "jiuwenswarm", request_id="bind-readonly")


def test_self_bind_rejects_other_connector(runtime_factory):
    api = _bootstrap(runtime_factory(self_bind=True))
    _member(api, SWARM)
    task = _task(api, _security(OWNER))
    with pytest.raises(PermissionDeniedError):
        _bind(api, _security(SWARM), task, "hermes")


def test_self_unbind_only_own_binding(runtime_factory):
    api = _bootstrap(runtime_factory(self_bind=True))
    _member(api, SWARM)
    task = _task(api, _security(OWNER))
    task = _bind(
        api, _security(OWNER), task, "jiuwenswarm", request_id="bind-swarm", context_id="s1"
    )
    task = _bind(
        api, _security(OWNER), task, "hermes", request_id="bind-hermes", context_id="s1"
    )
    bindings = {b["connector_id"]: b["binding_id"] for b in task["task"]["bindings"]}

    unbind_own = TaskRequest(
        "unbind",
        "u-own",
        task["task_id"],
        task["revision"],
        binding_id=bindings["jiuwenswarm"],
    )
    result = api.continuity_task(PROJECT, unbind_own, security=_security(SWARM))
    assert result["revision"] == unbind_own.expected_revision + 1
    foreign = TaskRequest(
        "unbind",
        "u-foreign",
        result["task_id"],
        result["revision"],
        binding_id=bindings["hermes"],
    )
    with pytest.raises(PermissionDeniedError):
        api.continuity_task(PROJECT, foreign, security=_security(SWARM))


def test_close_still_owner_only_with_self_bind(runtime_factory):
    api = _bootstrap(runtime_factory(self_bind=True))
    _member(api, SWARM)
    task = _task(api, _security(OWNER))
    with pytest.raises(PermissionDeniedError):
        api.continuity_task(
            PROJECT,
            TaskRequest("close", "c1", task["task_id"], task["revision"]),
            security=_security(SWARM),
        )


def test_admin_manages_without_write(runtime_factory):
    api = _bootstrap(runtime_factory(admin_manage_tasks=True))
    _member(api, ADMIN, content=SpaceContentRole.NONE, governance=SpaceGovernanceRole.MANAGER)
    _member(api, SWARM)
    task = _task(api, _security(OWNER))
    closed = api.continuity_task(
        PROJECT,
        TaskRequest("close", "close-admin", task["task_id"], task["revision"]),
        security=_security(ADMIN),
    )
    assert closed["task"]["state"] == "closed", "space manager could not close a task"
    second = _task(api, _security(OWNER), request_id="t-second")
    bound = _bind(api, _security(ADMIN), second, "hermes", request_id="bind-admin")
    assert any(
        b["connector_id"] == "hermes" and b["active"] for b in bound["task"]["bindings"]
    ), "space manager could not bind for another connector"


def test_admin_cannot_write_events_without_write(runtime_factory):
    api = _bootstrap(runtime_factory(admin_manage_tasks=True))
    _member(api, ADMIN, content=SpaceContentRole.NONE, governance=SpaceGovernanceRole.MANAGER)
    task = _task(api, _security(OWNER))
    bound = _bind(api, _security(OWNER), task, "jiuwenswarm", context_id="s1")
    binding_id = bound["task"]["bindings"][-1]["binding_id"]
    write = ContinuityWrite(bound["task_id"], binding_id, "e1")
    with pytest.raises(PermissionDeniedError):
        api.add("admin tries to write", PROJECT, security=_security(ADMIN), continuity=write)


def test_admin_policy_off_rejects_non_owner_admin(runtime_factory):
    api = _bootstrap(runtime_factory())
    _member(api, ADMIN, content=SpaceContentRole.NONE, governance=SpaceGovernanceRole.MANAGER)
    task = _task(api, _security(OWNER))
    with pytest.raises(PermissionDeniedError):
        api.continuity_task(
            PROJECT,
            TaskRequest("close", "close-admin", task["task_id"], task["revision"]),
            security=_security(ADMIN),
        )
