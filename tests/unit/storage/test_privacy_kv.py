"""Failure recovery preserves the private binding of surviving truth."""

import copy
from dataclasses import replace

import pytest

from jiuwen_memory.common.security.privacy.privacy_impl.regex import RegexPrivacy
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment

pytestmark = pytest.mark.unit
SCOPE = Scope(org="o", user="alice")
TEXT = "联系我：13800138000，api_key=private-demo-value"


def test_partial_kv_failure_preserves_masked_truth_and_matching_disclosure(privacy, monkeypatch):
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    raw = InMemoryKVStore()
    insert = raw.insert

    def partial(scope, key, value, ttl=0.0):
        insert(scope, key, value, ttl)
        raise BackendError("injected partial failure")

    monkeypatch.setattr(raw, "insert", partial)
    store = PrivacyKVStore(raw, privacy)
    unit = MemoryUnit(id="partial", scope=SCOPE, segments=[Segment(TEXT)])
    with pytest.raises(BackendError):
        store.insert(SCOPE, "/memory/partial", dumps(unit))
    remaining = loads(raw.get(SCOPE, "/memory/partial"))
    assert "private-demo-value" not in remaining.content
    assert privacy.render(remaining) == TEXT


@pytest.mark.parametrize("channel,prefix", [("memory", "/memory/"), ("message", "/messages/")])
@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
def test_kv_failure_preserves_old_private_snapshot(
    privacy, monkeypatch, channel, prefix, operation, privacy_factory
):
    from jiuwen_memory.common.errors import BackendError, ConflictError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    raw = InMemoryKVStore()
    store = PrivacyKVStore(raw, privacy)
    key = prefix + "existing"
    old = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment(TEXT)])
    store.insert(SCOPE, key, dumps(old))
    old_bytes = raw.get(SCOPE, key)
    old = loads(old_bytes)

    def fail(*args, **kwargs):
        raise BackendError("injected before truth mutation")

    if operation != "insert":
        monkeypatch.setattr(raw, operation, fail)
    changed = MemoryUnit(id=old.id, scope=SCOPE, segments=[Segment("新电话13800138001")])
    with pytest.raises(ConflictError if operation == "insert" else BackendError):
        if operation == "delete":
            store.delete(SCOPE, key)
        else:
            getattr(store, operation)(SCOPE, key, dumps(changed))
    assert raw.get(SCOPE, key) == old_bytes
    if channel == "memory":
        assert privacy.render(old) == TEXT
    else:
        derived = replace(copy.deepcopy(old), id="derived", provenance=[old.id])
        privacy.close()
        privacy = privacy_factory()
        privacy.persist(derived)
        assert privacy.render(derived) == TEXT


@pytest.mark.parametrize("committed", [False, True])
def test_unavailable_readback_retains_both_candidates_across_restart(
    privacy, monkeypatch, tmp_path, committed, privacy_state, privacy_crypto
):
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    raw = InMemoryKVStore()
    store = PrivacyKVStore(raw, privacy)
    key = "/memory/uncertain"
    initial = MemoryUnit(id="uncertain", scope=SCOPE, segments=[Segment(TEXT)])
    store.insert(SCOPE, key, dumps(initial))
    update, get = raw.update, raw.get

    def partial(scope, key, value, ttl=0.0):
        if committed:
            update(scope, key, value, ttl)
        raise BackendError("write outcome unknown")

    def unavailable(*args):
        raise BackendError("read unavailable")

    monkeypatch.setattr(raw, "update", partial)
    monkeypatch.setattr(raw, "get", unavailable)
    changed = MemoryUnit(id="uncertain", scope=SCOPE, segments=[Segment("新电话13800138001")])
    with pytest.raises(BackendError, match="write outcome"):
        store.update(SCOPE, key, dumps(changed))
    surviving = loads(get(SCOPE, key))
    reopened = RegexPrivacy(privacy_crypto, str(tmp_path / "private.sqlite"))
    try:
        assert reopened.render(surviving) == ("新电话13800138001" if committed else TEXT)
        monkeypatch.setattr(raw, "update", update)
        monkeypatch.setattr(raw, "get", get)
        # A later successful retry authenticates references even when they
        # belonged to the recovery candidate rather than the active manifest.
        store.update(SCOPE, key, dumps(surviving))
        expected = "新电话13800138001" if committed else TEXT
        assert privacy.render(loads(get(SCOPE, key))) == expected
        assert privacy_state.execute("SELECT COUNT(*) FROM recovery_manifests").fetchone()[0] == 0
    finally:
        reopened.close()


def test_confirmed_partial_update_collects_only_the_obsolete_snapshot(
    privacy, monkeypatch, privacy_state
):
    from jiuwen_memory.common.errors import BackendError, ValidationError
    from jiuwen_memory.common.type_def.memory_codec import dumps, loads
    from jiuwen_memory.storage._privacy_kv import PrivacyKVStore
    from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

    raw = InMemoryKVStore()
    store = PrivacyKVStore(raw, privacy)
    key = "/memory/updated"
    store.insert(SCOPE, key, dumps(MemoryUnit(id="updated", scope=SCOPE, segments=[Segment(TEXT)])))
    old, update = loads(raw.get(SCOPE, key)), raw.update

    def partial(scope, key, value, ttl=0.0):
        update(scope, key, value, ttl)
        raise BackendError("committed before failure")

    monkeypatch.setattr(raw, "update", partial)
    changed = MemoryUnit(id="updated", scope=SCOPE, segments=[Segment("新电话13800138001")])
    with pytest.raises(BackendError):
        store.update(SCOPE, key, dumps(changed))
    assert privacy.render(loads(raw.get(SCOPE, key))) == "新电话13800138001"
    with pytest.raises(ValidationError):
        privacy.render(old)
    assert privacy_state.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == 1
    assert privacy_state.execute("SELECT COUNT(*) FROM recovery_manifests").fetchone()[0] == 0
    store.delete(SCOPE, key)
    assert privacy_state.execute("SELECT COUNT(*) FROM fragments").fetchone()[0] == 0
