# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开增量能力、原子创建和资源所有权契约回归。"""

import asyncio
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.security.authorization.authorization_impl.memory_stores import (
    InMemoryGrantStore,
)
from jiuwen_memory.common.security.authorization.authorization_impl.sqlite_stores import (
    SQLiteGrantStore,
)
from jiuwen_memory.common.security.capabilities import (
    SecurityResourceOwner,
    credential_sources,
    delegation_sources,
    needs_space_facts,
)
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


@pytest.fixture(name="managed_store", params=["memory", "sqlite"])
def _managed_store(request):
    store = InMemoryGrantStore() if request.param == "memory" else SQLiteGrantStore(":memory:")
    yield store
    if request.param == "sqlite":
        store.close()


def _record():
    return Grant(
        grant_id="created-once",
        grantor=Scope(org="review", user="alice"),
        grantee=Scope(org="review", user="bob"),
        actions=frozenset({Action.READ}),
    )


def test_atomic_creation_retries_same_id_without_replacing_grant(managed_store):
    record = _record()
    managed_store.create_grant(record)
    managed_store.create_grant(record)
    assert managed_store.get_grant(record.grant_id) == record
    with pytest.raises(BackendError, match="conflict"):
        managed_store.create_grant(replace(record, grantor=Scope(org="other", user="victim")))
    assert managed_store.get_grant(record.grant_id) == record


def test_atomic_creation_cannot_resurrect_a_revoked_id(managed_store):
    record = _record()
    managed_store.create_grant(record)
    managed_store.revoke(record.grant_id)
    with pytest.raises(BackendError, match="conflict"):
        managed_store.create_grant(record)
    assert managed_store.get_grant(record.grant_id).revoked


def test_sqlite_failure_after_insert_rolls_back_and_can_retry(monkeypatch):
    store = SQLiteGrantStore(":memory:")
    record = _record()
    original_add = store.add

    def interrupted_add(grant):
        original_add(grant)
        raise BackendError("injected failure after insert")

    try:
        with monkeypatch.context() as injection:
            injection.setattr(store, "add", interrupted_add)
            with pytest.raises(BackendError, match="after insert"):
                store.create_grant(record)
        assert store.get_grant(record.grant_id) is None, "uncommitted grant must not survive"
        store.create_grant(record)
        assert store.get_grant(record.grant_id) == record
    finally:
        store.close()


def test_memory_creation_copy_failure_does_not_publish_grant(monkeypatch):
    store = InMemoryGrantStore()
    record = _record()

    def interrupted_copy(grant):
        raise BackendError("injected copy failure")

    with monkeypatch.context() as injection:
        injection.setattr(
            "jiuwen_memory.common.security.authorization.authorization_impl.memory_stores.deepcopy",
            interrupted_copy,
        )
        with pytest.raises(BackendError, match="copy failure"):
            store.create_grant(record)
    assert store.get_grant(record.grant_id) is None
    store.create_grant(record)
    assert store.get_grant(record.grant_id) == record


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, KeyboardInterrupt])
def test_sqlite_interruption_rolls_back_and_allows_retry(monkeypatch, interruption):
    store = SQLiteGrantStore(":memory:")
    record = _record()
    original_add = store.add

    def interrupted_add(grant):
        original_add(grant)
        raise interruption("interrupted before commit")

    try:
        with monkeypatch.context() as injection:
            injection.setattr(store, "add", interrupted_add)
            with pytest.raises(interruption):
                store.create_grant(record)
        assert store.get_grant(record.grant_id) is None, "interruption must not publish authority"
        store.create_grant(record)
        assert store.get_grant(record.grant_id) == record
    finally:
        store.close()


class TrackedResource:
    def __init__(self, name, events, dependencies=()):
        self.name = name
        self.events = events
        self.dependencies = dependencies

    def security_dependencies(self):
        return self.dependencies

    def close(self):
        self.events.append(self.name)


def test_resource_owner_closes_shared_dependencies_once_in_dependency_order():
    events = []
    shared = TrackedResource("shared", events)
    first = TrackedResource("first", events, (shared,))
    second = TrackedResource("second", events, (shared,))
    owner = SecurityResourceOwner((first, second, first))
    owner.close()
    owner.close()
    assert events.count("shared") == 1
    assert events.index("shared") > events.index("first")
    assert events.index("shared") > events.index("second")


def test_resource_owner_continues_and_retries_failed_close():
    events = []

    class Interrupted(TrackedResource):
        attempts = 0

        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise BackendError("injected close failure")
            super().close()

    stable = TrackedResource("stable", events)
    interrupted = Interrupted("interrupted", events)
    owner = SecurityResourceOwner((stable, interrupted))
    owner.close()
    assert events == ["stable"]
    owner.close()
    assert events == ["stable", "interrupted"]


def test_resource_owner_does_not_close_borrowed_resources():
    events = []
    borrowed = TrackedResource("borrowed", events)
    owner = SecurityResourceOwner((TrackedResource("owner", events),))
    owner.close()
    assert events == ["owner"]
    borrowed.close()
    assert events == ["owner", "borrowed"]


@pytest.mark.parametrize("invalid", [None, [], (None,)])
def test_invalid_resource_declaration_fails_at_assembly(invalid):
    with pytest.raises(ValidationError, match="resource tuple"):
        SecurityResourceOwner((TrackedResource("invalid", [], invalid),))


def test_cyclic_resource_declaration_fails_at_assembly():
    resource = TrackedResource("cycle", [])
    resource.dependencies = (resource,)
    with pytest.raises(ValidationError, match="cyclic"):
        SecurityResourceOwner((resource,))


def test_invalid_space_facts_declaration_is_rejected():
    class Invalid:
        @staticmethod
        def requires_space_facts():
            return "true"

    with pytest.raises(ValidationError, match="return bool"):
        needs_space_facts(Invalid())


def test_invalid_delegation_declaration_is_rejected():
    class Invalid:
        @staticmethod
        def delegation_stores():
            return (object(),)

    with pytest.raises(ValidationError, match="DelegationStore"):
        delegation_sources(Invalid())


def test_invalid_credential_declaration_is_rejected():
    class Invalid:
        @staticmethod
        def credential_sources():
            return (("api_key", "issuer", object()),)

    with pytest.raises(ValidationError, match="PrincipalKeyStore"):
        credential_sources(Invalid())


def test_explicit_runtime_owners_share_resource_release():
    events = []
    shared = TrackedResource("shared", events)
    left = SecurityResourceOwner((TrackedResource("left", events, (shared,)),))
    right = SecurityResourceOwner((TrackedResource("right", events, (shared,)),))
    left.share_with(right)
    right.close()
    left.close()
    assert events.count("shared") == 1
    assert events.index("shared") > events.index("left")
    assert events.index("shared") > events.index("right")


def test_closed_lifecycle_cannot_be_adopted():
    left = SecurityResourceOwner(())
    right = SecurityResourceOwner(())
    right.close()
    with pytest.raises(ValidationError, match="closed security lifecycle"):
        left.share_with(right)


def test_nested_lifecycle_roots_are_rejected_instead_of_recursively_closing():
    class Nested:
        @staticmethod
        def security_resource_owner():
            return SecurityResourceOwner(())

    with pytest.raises(ValidationError, match="nested lifecycle"):
        SecurityResourceOwner((Nested(),))


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, KeyboardInterrupt])
def test_interrupted_close_still_cleans_other_resources_and_can_retry(interruption):
    events = []

    class Interrupted(TrackedResource):
        attempts = 0

        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise interruption("interrupted closing resource")
            super().close()

    owner = SecurityResourceOwner(
        (TrackedResource("stable", events), Interrupted("interrupted", events))
    )
    with pytest.raises(interruption):
        owner.close()
    assert events == ["stable"]
    owner.close()
    assert events == ["stable", "interrupted"]


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, KeyboardInterrupt])
def test_interrupted_close_notification_does_not_skip_resource_cleanup(interruption):
    events = []
    attempts = []

    def notify():
        attempts.append(True)
        if len(attempts) == 1:
            raise interruption("interrupted close notification")

    owner = SecurityResourceOwner((TrackedResource("resource", events),))
    owner.add_close_callback(notify)
    with pytest.raises(interruption):
        owner.close()
    assert events == ["resource"]
    owner.close()
    assert len(attempts) == 2
    assert events == ["resource"]
