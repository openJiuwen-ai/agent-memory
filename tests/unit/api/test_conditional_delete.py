"""Conditional delete checks the expected content digest before removing a unit."""

from __future__ import annotations

import hashlib

import pytest

from jiuwen_memory.api import DeleteSelector, Scope, assemble
from jiuwen_memory.common.errors import UnsupportedCapabilityError
from jiuwen_memory.common.security import internal_context
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def test_expected_digest_mismatch_keeps_unit():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    unit = api.add("keep me", scope, security=security)[0]
    with pytest.raises(UnsupportedCapabilityError):
        api.delete(
            DeleteSelector(
                unit_ids=[unit.id],
                scope=scope,
                expected_digests={unit.id: hashlib.sha256(b"different").hexdigest()},
            ),
            security=security,
        )
    assert api.get(unit.id, scope=scope, security=security) is not None


def test_expected_digest_match_deletes_unit():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    security = internal_context(ScopedAuthenticator(scope))
    unit = api.add("delete me", scope, security=security)[0]
    digest = hashlib.sha256(b"delete me").hexdigest()
    with pytest.raises(UnsupportedCapabilityError):
        api.delete(
            DeleteSelector(
                unit_ids=[unit.id], scope=scope, expected_digests={unit.id: digest}
            ),
            security=security,
        )
    assert api.get(unit.id, scope=scope, security=security) is not None
