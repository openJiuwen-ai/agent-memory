"""KVStore.mget 批量点读契约测试（in_memory / sqlite）。

覆盖 ``mget``：返回与 ``keys`` 下标一一对应的 ``list[bytes]``——任一 key 缺失即报
``NotFoundError``（与 ``get`` 一致）、不去重、支持重复 key（各下标独立返回，语义同
Redis ``MGET``）、scope 隔离、空入参短路。encrypted 后端的 ``mget`` 在
``test_encrypted_kv_store.py`` 覆盖（复用其 ``_FakeSecurity``）。
"""

from __future__ import annotations

import pytest

from common.errors import NotFoundError
from common.type_def import Scope, memory_key
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", user="alice")
_OTHER = Scope(org="acme", user="bob")


def _backends(tmp_path):
    return [InMemoryKVStore(), SQLiteKVStore(str(tmp_path / "kv.sqlite3"))]


def test_mget_returns_all_hits_positionally(tmp_path) -> None:
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("u1"), b"one")
        store.insert(_SCOPE, memory_key("u2"), b"two")

        out = store.mget(_SCOPE, [memory_key("u1"), memory_key("u2")])

        assert out == [b"one", b"two"]


def test_mget_empty_keys_returns_empty_list(tmp_path) -> None:
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("u1"), b"x")

        assert store.mget(_SCOPE, []) == []


def test_mget_does_not_cross_scope(tmp_path) -> None:
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("shared"), b"mine")
        store.insert(_OTHER, memory_key("shared"), b"theirs")

        assert store.mget(_SCOPE, [memory_key("shared")]) == [b"mine"]
        assert store.mget(_OTHER, [memory_key("shared")]) == [b"theirs"]


def test_mget_supports_duplicate_keys_positionally(tmp_path) -> None:
    """mget 不去重；重复 key 各下标独立返回对应值。"""
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("u1"), b"v1")

        out = store.mget(_SCOPE, [memory_key("u1"), memory_key("u1"), memory_key("u1")])

        assert out == [b"v1", b"v1", b"v1"]


def test_mget_missing_raises_not_found_like_get(tmp_path) -> None:
    """缺失语义与 get 一致：任一 key 不存在即报 NotFoundError。"""
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("hit"), b"ok")

        # 仅查缺失 → 报错
        with pytest.raises(NotFoundError):
            store.mget(_SCOPE, [memory_key("ghost")])

        # 命中后跟一个缺失 → 整体报错（批量不静默省略）
        with pytest.raises(NotFoundError):
            store.mget(_SCOPE, [memory_key("hit"), memory_key("ghost")])
        with pytest.raises(NotFoundError):
            store.mget(_SCOPE, [memory_key("ghost"), memory_key("hit")])


def test_mget_missing_key_recorded_in_error(tmp_path) -> None:
    """报错信息携带缺失的 key，便于定位是哪个 key 不存在。"""
    for store in _backends(tmp_path):
        store.insert(_SCOPE, memory_key("u1"), b"ok")  # 命中；ghost 缺失
        with pytest.raises(NotFoundError) as exc_info:
            store.mget(_SCOPE, [memory_key("u1"), memory_key("ghost")])

        assert "ghost" in str(exc_info.value)
