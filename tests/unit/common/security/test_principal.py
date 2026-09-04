from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.principal import (
    author_match,
    covers_owner,
    derive_author,
    owner_entry_of,
    require_principal,
    same_dims,
)
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


def test_derive_author_covers_six_identity_shapes() -> None:
    # 代理链上有人类主体即归人类，代理项记录经哪个代理写入
    assert derive_author(Scope(user="alice")) == ("user:alice", "")
    assert derive_author(Scope(user="alice", agent="a1")) == ("user:alice", "a1")
    assert derive_author(Scope(org="acme", user="alice", session="s1")) == ("user:alice", "")
    # 代理自主运行
    assert derive_author(Scope(agent="a1")) == ("agent:a1", "")
    assert derive_author(Scope(org="acme", agent="a1", session="s1")) == ("agent:a1", "")
    # 主体两维全空即无从推导
    with pytest.raises(ValidationError):
        derive_author(Scope(org="acme"))


def test_require_principal_rejects_empty_principal_dims() -> None:
    require_principal(Scope(user="alice"))
    require_principal(Scope(agent="a1"))
    with pytest.raises(PermissionDeniedError):
        require_principal(Scope(org="acme", space="s"))


def test_owner_entry_of_takes_a_single_dimension() -> None:
    assert owner_entry_of(Scope(user="alice", agent="a1"), "acme", "u-alice") == Scope(
        org="acme", space="u-alice", user="alice"
    )
    assert owner_entry_of(Scope(agent="a1"), "acme", "a-a1") == Scope(
        org="acme", space="a-a1", agent="a1"
    )
    assert owner_entry_of(Scope(org="acme"), "acme", "ops") is None


def test_comparisons_ignore_space_dimension() -> None:
    """登记项带 space 维、调用方身份不带，是两侧的常态形态。

    比较该维即恒为假，归属对比与归属主体档两级一并失效——用户读写自己的主空间被拒，
    且症状与「回填未完成」不可区分。
    """
    entry = Scope(org="acme", space="u-alice", user="alice")
    actor = Scope(org="acme", user="alice")
    assert covers_owner(entry, actor) is True
    assert same_dims(entry, actor) is True


def test_covers_owner_is_a_coarse_filter_and_same_dims_is_exact() -> None:
    entry = Scope(org="acme", space="u-alice", user="alice")
    via_agent = Scope(org="acme", user="alice", agent="a1")
    # 用户经其名下代理调用：覆盖成立、逐维相同不成立
    assert covers_owner(entry, via_agent) is True
    assert same_dims(entry, via_agent) is False
    # 另一个用户：两者都不成立
    other = Scope(org="acme", user="bob")
    assert covers_owner(entry, other) is False
    assert same_dims(entry, other) is False
    # 跨 org
    assert covers_owner(entry, Scope(org="other", user="alice")) is False


def test_author_match_only_compares_the_principal_and_ignores_the_agent_dimension() -> None:
    """同一用户名下换代理不改变可达性：条目可读范围由空间权限决定，条目上无可见性声明。"""
    actor_direct = Scope(org="acme", user="alice")
    actor_via_a1 = Scope(org="acme", user="alice", agent="a1")
    actor_via_a2 = Scope(org="acme", user="alice", agent="a2")

    # 三种形态推导出同一个作者主体，比对结果必须一致
    assert author_match(actor_direct, "user:alice") is True
    assert author_match(actor_via_a1, "user:alice") is True
    assert author_match(actor_via_a2, "user:alice") is True
    # 主体项不同即不成立；条目没有作者标记时具名调用方也不成立
    assert author_match(actor_direct, "user:bob") is False
    assert author_match(actor_direct, "") is False
