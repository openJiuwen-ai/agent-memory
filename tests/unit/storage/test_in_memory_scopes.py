# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""InMemoryKVStore.scopes：读/删缺失键不得凭空制造 scope，删空后 scope 应消失。"""

import pytest

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

SCOPE = Scope(org="o", space="s", user="u", agent="a", session="x")


def test_reads_and_missing_delete_do_not_create_scope() -> None:
    store = InMemoryKVStore()
    with pytest.raises(NotFoundError):
        store.get(SCOPE, memory_key("missing"))
    assert store.exists(SCOPE, memory_key("missing")) is False
    assert store.scan(SCOPE) == []
    store.delete(SCOPE, memory_key("missing"))
    assert store.scopes() == []


def test_scope_disappears_after_last_key_is_deleted() -> None:
    store = InMemoryKVStore()
    store.insert(SCOPE, memory_key("u1"), b"v")
    assert store.scopes() == [SCOPE]
    store.delete(SCOPE, memory_key("u1"))
    assert store.scopes() == []
