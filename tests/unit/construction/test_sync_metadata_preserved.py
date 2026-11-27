"""Creation-only sync attribution: writable at creation, never rewritten afterwards."""

from __future__ import annotations

import pytest

from jiuwen_memory.api import (
    BatchWriteItem,
    MemoryPatch,
    Scope,
    UpdateSelector,
    ValidationError,
    assemble,
)
from jiuwen_memory.common.security import internal_context
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _api_and_scope():
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    return api, scope, internal_context(ScopedAuthenticator(scope))


def test_update_keeps_creation_metadata():
    api, scope, security = _api_and_scope()
    # Creation-only attribution travels in system_metadata through the ordinary add.
    unit = api.add(
        "original", scope, security=security,
        system_metadata={"sync_created_by": "op-1", "sync_initial_digest": "d1"},
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
    unit = api.add(
        "original", scope, security=security,
        system_metadata={"sync_created_by": "op-1"},
    )[0]
    api.batch_update(
        UpdateSelector(unit_ids=[unit.id], scope=scope),
        [MemoryPatch(content="changed", system_metadata={"other": "kept"})],
        security=security,
    )
    current = api.get(unit.id, scope=scope, security=security)
    assert current.system_metadata["sync_created_by"] == "op-1"


def test_update_rejects_rewrite_of_creation_metadata():
    api, scope, security = _api_and_scope()
    unit = api.add(
        "original", scope, security=security,
        system_metadata={"sync_created_by": "op-1"},
    )[0]
    with pytest.raises(ValidationError, match="sync_created_by"):
        api.update(
            unit.id,
            scope,
            MemoryPatch(system_metadata={"sync_created_by": "op-2"}),
            security=security,
        )
    with pytest.raises(ValidationError, match="sync_created_by"):
        api.batch_update(
            UpdateSelector(unit_ids=[unit.id], scope=scope),
            [MemoryPatch(system_metadata={"sync_created_by": "op-2"})],
            security=security,
        )
    current = api.get(unit.id, scope=scope, security=security)
    assert current.system_metadata["sync_created_by"] == "op-1"


def test_batch_add_accepts_creation_metadata():
    api, scope, security = _api_and_scope()
    outcomes = api.batch_add(
        [
            BatchWriteItem(content="inherited"),
            BatchWriteItem(
                content="overridden",
                system_metadata={"sync_created_by": "op-item"},
            ),
        ],
        scope,
        security=security,
        system_metadata={"sync_created_by": "op-batch"},
    ).outcomes
    assert [outcome.error for outcome in outcomes] == ["", ""]
    by_content = {outcome.item.content: outcome.units[0] for outcome in outcomes}
    assert by_content["inherited"].system_metadata["sync_created_by"] == "op-batch"
    assert by_content["overridden"].system_metadata["sync_created_by"] == "op-item"
