"""GrantStore / DelegationStore 契约形状与 Producer 顶层段名。

接口先行版：``authorization_impl`` 的 memory / SQLite 实现未合入，只固定
两个真源 ABC 的抽象契约。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)

pytestmark = pytest.mark.unit


def test_grant_store_cannot_be_partially_implemented() -> None:
    class Incomplete(GrantStore):
        def add(self, grant):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_delegation_store_cannot_be_partially_implemented() -> None:
    class Incomplete(DelegationStore):
        def add(self, delegation):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_producers_declare_distinct_top_names() -> None:
    """Grant 与 Delegation 使用不同真源，Producer 顶层段名也必须独立。"""
    assert GrantStoreProducer.TOP_NAME == "grant_store"
    assert DelegationStoreProducer.TOP_NAME == "delegation_store"
    assert GrantStoreProducer.TOP_NAME != DelegationStoreProducer.TOP_NAME
