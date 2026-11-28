"""Ordinary add can persist and replay an operation receipt."""

from __future__ import annotations

import pytest

from jiuwen_memory.api import Scope, assemble
from jiuwen_memory.common.security import internal_context
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def test_receipt_lists_outputs_and_replays_without_writing():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    first = api.add("hello", scope, security=security, operation_id="op-1")
    receipt = api.operation_receipt("op-1", scope, security=security)
    assert receipt is not None
    assert [item["unit_id"] for item in receipt.outputs] == [first[0].id]
    replay = api.add("hello", scope, security=security, operation_id="op-1")
    assert [item.id for item in replay] == [first[0].id]
    assert len(api.list(scope, security=security).items) == 1


def test_no_operation_id_keeps_old_behavior():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    api.add("hello", scope, security=security)
    assert api.operation_receipt("missing", scope, security=security) is None


def test_receipt_is_isolated_by_subject():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    first = api.add("hello", scope, security=security, operation_id="op-auth")
    other_scope = Scope(org="acme", user="intruder")
    other = internal_context(ScopedAuthenticator(other_scope))
    assert api.operation_receipt("op-auth", other_scope, security=other) is None
    other_units = api.add("hello", other_scope, security=other, operation_id="op-auth")
    assert other_units[0].id != first[0].id
    assert api.get(first[0].id, scope=scope, security=security) is not None


def test_receipt_is_isolated_by_org():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    first = api.add("hello", scope, security=security, operation_id="op-2")
    other_scope = Scope(org="other", user="owner")
    other = internal_context(ScopedAuthenticator(other_scope))
    other_units = api.add("hello", other_scope, security=other, operation_id="op-2")
    assert other_units[0].id != first[0].id
    assert api.operation_receipt("op-2", other_scope, security=other) is not None
