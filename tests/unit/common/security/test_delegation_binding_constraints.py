# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""委托绑定条件拆分后的逐项失闭回归，不依赖后续资源授权补拒。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security._delegation_binding import _bound_record
from jiuwen_memory.common.security.authorization.authorization_impl.memory_stores import (
    InMemoryDelegationStore,
)
from jiuwen_memory.common.security.types import Action, AuthContext, Delegation, Role
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _valid_binding():
    auth = AuthContext(
        actor=Scope(org="acme", agent="bot", session="session"),
        credential_id="fingerprint",
        delegation_id="delegation",
    )
    record = Delegation(
        delegation_id="delegation",
        delegator=Scope(org="acme", user="alice"),
        delegate=Scope(org="acme", agent="bot"),
        actions=frozenset({Action.WRITE}),
        expires_at=NOW + timedelta(hours=1),
        allowed_spaces=frozenset({"main"}),
        bound_credential_id="fingerprint",
        bound_session="session",
    )
    return auth, record


@pytest.mark.parametrize(
    "auth_updates,record_updates,target",
    [
        ({"expires_at": NOW}, {}, Scope(org="acme", space="main")),
        ({"role": Role.ROOT}, {}, Scope(org="acme", space="main")),
        ({"actor": Scope(org="acme")}, {}, None),
        ({"actor": Scope(org="acme", user="alice", agent="bot")}, {}, None),
        ({}, {"revoked": True}, None),
        ({}, {"expires_at": NOW}, None),
        ({}, {"not_before": NOW + timedelta(seconds=1)}, None),
        ({}, {"delegate": Scope(org="acme")}, None),
        ({}, {"delegate": Scope(org="acme", user="alice", agent="bot")}, None),
        ({}, {"delegate": Scope(org="acme", agent="other")}, None),
        ({}, {"delegator": Scope(org="other", user="alice")}, None),
        ({}, {"bound_credential_id": "other"}, None),
        ({}, {"bound_session": "other"}, None),
        ({}, {"actions": frozenset({Action.READ})}, None),
        ({}, {}, Scope(org="other", space="main")),
        (
            {"actor": Scope(org="acme", space="other", agent="bot", session="session")},
            {},
            Scope(org="acme", space="main"),
        ),
        (
            {},
            {"delegator": Scope(org="acme", space="other", user="alice")},
            Scope(org="acme", space="main"),
        ),
        ({}, {}, Scope(org="acme", space="other")),
    ],
)
def test_each_binding_constraint_denies_independently(auth_updates, record_updates, target):
    auth, record = _valid_binding()
    store = InMemoryDelegationStore()
    store.add(replace(record, **record_updates))
    with pytest.raises(PermissionDeniedError, match="delegation_invalid"):
        _bound_record(
            replace(auth, **auth_updates), [store], now=NOW, action=Action.WRITE, target=target
        )


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_binding_is_denied(count):
    auth, record = _valid_binding()
    stores = [InMemoryDelegationStore() for _ in range(count)]
    for store in stores:
        store.add(record)
    with pytest.raises(PermissionDeniedError, match="delegation_invalid"):
        _bound_record(auth, stores, now=NOW)


@pytest.mark.parametrize("restricted", [True, False])
def test_valid_binding_preserves_optional_restrictions(restricted):
    auth, record = _valid_binding()
    if not restricted:
        record = replace(
            record, allowed_spaces=frozenset(), bound_credential_id="", bound_session=""
        )
    store = InMemoryDelegationStore()
    store.add(record)
    assert _bound_record(auth, [store], now=NOW) == record
    assert (
        _bound_record(
            auth, [store], now=NOW, action=Action.WRITE, target=Scope(org="acme", space="main")
        )
        == record
    )
