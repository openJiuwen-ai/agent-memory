"""Failure recovery preserves the private binding of surviving truth."""

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", user="alice")
TEXT = "联系我：13800138000，api_key=private-demo-value"


@pytest.mark.parametrize("operation", ["build", "update", "remove", "remove_with_scope"])
@pytest.mark.parametrize("truth_written", [False, True])
def test_builder_failure_keeps_each_surviving_truth_revision(
    privacy, monkeypatch, operation, truth_written
):
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

    raw = InMemoryKVStore()
    kv = PrivacyKVStore(raw, privacy)
    old = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment(TEXT)])
    kv.insert(SCOPE, "/memory/existing", dumps(old))
    forward = ForwardIndexBuilder(CompositeStoreManager(kv=kv))
    write_truth = forward.update
    builder = PrivacyIndexBuilder(PrivacyIndexBuilder(forward, privacy), privacy)
    changed = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment("新电话13800138001")])

    def fail(units, *args, **kwargs):
        if truth_written and operation in {"build", "update"}:
            # Build may have delivered another unit of the batch before failing.
            write_truth(units)
        raise BackendError("injected index failure")

    if operation == "remove_with_scope":
        monkeypatch.setattr(forward, operation, fail, raising=False)
    else:
        monkeypatch.setattr(forward, operation, fail)
    with pytest.raises(BackendError):
        if operation == "remove_with_scope":
            builder.remove_with_scope([old.id], SCOPE)
        else:
            getattr(builder, operation)([changed])
    surviving = loads(raw.get(SCOPE, "/memory/existing"))
    expected = "新电话13800138001" if truth_written and operation in {"build", "update"} else TEXT
    assert privacy.render(surviving) == expected


def test_successful_projection_only_removal_keeps_truth_private_state(privacy, monkeypatch):
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

    old = MemoryUnit(id="u1", scope=SCOPE, segments=[Segment(TEXT)])
    privacy.persist(old)
    forward = ForwardIndexBuilder(CompositeStoreManager(kv=InMemoryKVStore()))
    monkeypatch.setattr(forward, "remove_with_scope", lambda ids, scope: None, raising=False)
    PrivacyIndexBuilder(forward, privacy).remove_with_scope([old.id], SCOPE)
    assert privacy.render(old) == TEXT


def test_index_only_builder_rejects_unbound_reference_before_delegate(privacy, monkeypatch):
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
        ForwardIndexBuilder,
    )
    from jiuwen_memory.construction.privacy_index_builder import PrivacyIndexBuilder
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
    from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
    from jiuwen_memory.storage.types import IndexWriteMode

    forward = ForwardIndexBuilder(CompositeStoreManager(kv=InMemoryKVStore()))
    called = []
    monkeypatch.setattr(forward, "build", lambda *args, **kwargs: called.append(True))
    unit = MemoryUnit(
        id="foreign", scope=SCOPE, segments=[Segment("[JW-PRIVATE:" + "a" * 48 + "]")]
    )
    with pytest.raises(ValidationError, match="unbound"):
        PrivacyIndexBuilder(forward, privacy).build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
    assert not called
