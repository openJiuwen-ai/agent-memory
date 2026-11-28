"""Creation-only sync attribution cannot be rewritten by update or batch_update."""

from __future__ import annotations

import pytest

from jiuwen_memory.api import MemoryPatch, Scope, UpdateSelector, assemble
from jiuwen_memory.common.security import internal_context
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _api_and_scope():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    return api, scope, internal_context(ScopedAuthenticator(scope))


def test_update_keeps_creation_metadata():
    api, scope, security = _api_and_scope()
    # Sync attribution can only be seeded through the internal writer.
    unit = api.add_sync(
        "original", scope, security=security,
        trusted_metadata={"sync_created_by": "op-1", "sync_initial_digest": "d1"},
    )[0]
    patched = api.update(
        unit.id,
        scope,
        MemoryPatch(
            content="changed",
            system_metadata={"other": "kept"},
        ),
        security=security,
    )
    assert patched.system_metadata["sync_created_by"] == "op-1"
    assert patched.system_metadata["sync_initial_digest"] == "d1"
    assert patched.system_metadata["other"] == "kept"


def test_batch_update_keeps_creation_metadata():
    api, scope, security = _api_and_scope()
    # Sync attribution can only be seeded through the internal writer.
    unit = api.add_sync(
        "original", scope, security=security,
        trusted_metadata={"sync_created_by": "op-1"},
    )[0]
    api.batch_update(
        UpdateSelector(unit_ids=[unit.id], scope=scope),
        [MemoryPatch(content="changed", system_metadata={"other": "kept"})],
        security=security,
    )
    current = api.get(unit.id, scope=scope, security=security)
    assert current.system_metadata["sync_created_by"] == "op-1"
