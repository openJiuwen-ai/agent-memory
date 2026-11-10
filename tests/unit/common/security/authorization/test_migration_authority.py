"""Migration roles are a ceiling and cannot be extended through WRITE grants."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.security.authorization.authorization_impl.memory_stores import (
    InMemoryDelegationStore,
    InMemoryGrantStore,
)
from jiuwen_memory.common.security.authorization.authorization_impl.standard_authorizer import (
    StandardAuthorizer,
)
from jiuwen_memory.common.security.authorization.scope_rules import (
    migration_scope_allowed,
    scope_covers,
)
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    AuthorizationEnvironment,
    ResourceDescriptor,
    Role,
)
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "role,actor,target,allowed",
    [
        (Role.USER, Scope(org="a", user="alice"), Scope(org="a", user="alice"), True),
        (Role.USER, Scope(org="a", user="alice"), Scope(org="a", user="bob"), False),
        (Role.USER, Scope(org="a", user="alice"), Scope(org="b", user="alice"), False),
        (Role.USER, Scope(org="a", agent="bot"), Scope(org="a", agent="bot"), True),
        (
            Role.USER,
            Scope(org="a", user="alice", session="s"),
            Scope(org="a", user="alice", session="other"),
            False,
        ),
        (Role.ADMIN, Scope(org="a", user="admin"), Scope(org="a", user="bob"), True),
        (Role.ADMIN, Scope(org="a", user="admin"), Scope(org="b", user="bob"), False),
        (Role.ROOT, Scope(org="system", user="root"), Scope(org="b", user="bob"), True),
    ],
)
def test_migration_role_scope_matrix(role, actor, target, allowed):
    authorizer = StandardAuthorizer(InMemoryGrantStore(), InMemoryDelegationStore())
    decision = authorizer.authorize(
        auth=AuthContext(actor=actor, role=role),
        resource=ResourceDescriptor(
            scope=target, action=Action.MIGRATE_MEMORY, resource_type="memory_unit"
        ),
        environment=AuthorizationEnvironment(now=datetime.now(UTC)),
    )
    assert decision.allowed is allowed


def test_grant_cannot_extend_migration_ceiling():
    from jiuwen_memory.common.security.types import Grant

    grants = InMemoryGrantStore()
    grants.add(
        Grant(
            grantor=Scope(org="a", user="bob"),
            grantee=Scope(org="a", user="alice"),
            actions=frozenset({Action.MIGRATE_MEMORY, Action.WRITE}),
        )
    )
    decision = StandardAuthorizer(grants, InMemoryDelegationStore()).authorize(
        auth=AuthContext(actor=Scope(org="a", user="alice")),
        resource=ResourceDescriptor(
            action=Action.MIGRATE_MEMORY,
            resource_type="memory_unit",
            scope=Scope(org="a", user="bob"),
        ),
        environment=AuthorizationEnvironment(now=datetime.now(UTC)),
    )
    assert not decision.allowed


@pytest.mark.parametrize("dimension", ["space", "user", "agent", "session"])
@pytest.mark.parametrize("target_value", ["", "other", "bound"])
def test_migration_preserves_every_bound_user_dimension(dimension, target_value):
    actor = Scope(org="a", space="bound", user="bound", agent="bound", session="bound")
    target = replace(actor, **{dimension: target_value})
    auth = AuthContext(actor=actor, role=Role.USER)
    decision = StandardAuthorizer(InMemoryGrantStore(), InMemoryDelegationStore()).authorize(
        auth=auth,
        resource=ResourceDescriptor(
            scope=target, action=Action.MIGRATE_MEMORY, resource_type="memory_unit"
        ),
        environment=AuthorizationEnvironment(now=datetime.now(UTC)),
    )
    assert migration_scope_allowed(auth, target) is (target_value == "bound")
    assert decision.allowed is (target_value == "bound")


@pytest.mark.parametrize("principal", ["user", "agent"])
@pytest.mark.parametrize("actor_space", ["", "s", "other"])
@pytest.mark.parametrize("target_space", ["", "s", "other"])
def test_user_migration_matches_owner_space_boundary(principal, actor_space, target_space):
    actor = Scope(org="a", space=actor_space, **{principal: "alice"})
    target = replace(actor, space=target_space)
    auth = AuthContext(actor=actor, role=Role.USER)
    decision = StandardAuthorizer(InMemoryGrantStore(), InMemoryDelegationStore()).authorize(
        auth=auth,
        resource=ResourceDescriptor(
            scope=target, action=Action.MIGRATE_MEMORY, resource_type="memory_unit"
        ),
        environment=AuthorizationEnvironment(now=datetime.now(UTC)),
    )
    expected = actor_space == target_space
    assert scope_covers(actor, target) is expected
    assert migration_scope_allowed(auth, target) is expected
    assert decision.allowed is expected
