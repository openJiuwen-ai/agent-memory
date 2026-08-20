from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.space_impl.space_index import SpaceIndex
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


def _index() -> SpaceIndex:
    return SpaceIndex(InMemoryKVStore())


def test_add_and_remove_are_idempotent() -> None:
    index = _index()
    alice = Scope(org="acme", user="alice")

    index.add(alice, "u-alice")
    index.add(alice, "u-alice")  # 重复登记不报错、不重复
    assert index.spaces_for(alice, "acme") == ("u-alice",)

    index.remove(alice, "u-alice")
    index.remove(alice, "u-alice")  # 不存在即跳过
    assert index.spaces_for(alice, "acme") == ()


def test_spaces_for_merges_three_buckets_and_sorts() -> None:
    index = _index()
    index.add(Scope(org="acme", user="alice"), "u-alice")
    index.add(Scope(org="acme", agent="a1"), "a-a1")
    index.add(Scope(org="acme"), "org-all")  # 组织通配成员记录
    index.add(Scope(org="acme", user="bob"), "u-bob")

    # 带两维的调用方取两个桶的并集，加通配桶；返回值按空间名字典序
    assert index.spaces_for(Scope(org="acme", user="alice", agent="a1"), "acme") == (
        "a-a1",
        "org-all",
        "u-alice",
    )
    # 另一 org 的同名主体互不可见
    assert index.spaces_for(Scope(org="other", user="alice"), "other") == ()


def test_index_principal_must_carry_a_single_dimension() -> None:
    index = _index()
    with pytest.raises(ValidationError):
        index.add(Scope(org="acme", user="alice", agent="a1"), "team")


def test_remove_space_clears_every_entry_pointing_at_it() -> None:
    index = _index()
    index.add(Scope(org="acme", user="alice"), "team")
    index.add(Scope(org="acme", agent="a1"), "team")
    index.add(Scope(org="acme", user="alice"), "u-alice")

    assert index.remove_space("team") == 2
    assert index.spaces_for(Scope(org="acme", user="alice", agent="a1"), "acme") == ("u-alice",)

