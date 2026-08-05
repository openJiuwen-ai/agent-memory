"""Scope 覆盖规则（F05 §Authorization 决策顺序第 4 步）。

覆盖规则同时服务 owner 判定与 Grant 匹配，判错的后果是越权，故单独测。
"""

from __future__ import annotations

import pytest

from common.security.authorization.scope_rules import PrincipalPath, scope_covers
from common.type_def import Scope

# ====================================================================== #
# 硬边界
# ====================================================================== #


def test_cross_org_never_covers() -> None:
    assert not scope_covers(Scope(org="acme", user="alice"), Scope(org="other", user="alice"))


def test_cross_space_never_covers() -> None:
    """同 org 跨 space 也不覆盖：同名 user 在别的 space 不是同一份数据。"""
    parent = Scope(org="acme", space="s1", user="alice")
    child = Scope(org="acme", space="s2", user="alice")
    assert not scope_covers(parent, child)


# ====================================================================== #
# 空 Scope 不再是通配
# ====================================================================== #


def test_empty_scope_does_not_cover_everything() -> None:
    """与旧 ``_owner_scope_covers`` 的关键差异：空 parent 不再通配。

    旧实现用「parent == Scope() 即覆盖一切」同时表达 platform admin 与 grant 行的
    宽松匹配。F05 §授权不变量 1 要求 ROOT 只由 role 表达，这条通配分支必须消失——
    否则一个空 actor 或一条 grantor 留空的 Grant 就能横扫全平台。
    """
    assert not scope_covers(Scope(), Scope(org="acme", user="alice"))


def test_empty_scope_covers_only_empty_scope() -> None:
    assert scope_covers(Scope(), Scope())


def test_org_only_parent_does_not_cover_named_user() -> None:
    """留空最外层主体维不等于「不限该维」。

    ``Scope(org="acme")`` 覆盖不了 ``Scope(org="acme", user="alice")``：否则一条
    只写了 org 的记录就能读遍全 org。
    """
    assert not scope_covers(Scope(org="acme"), Scope(org="acme", user="alice"))


# ====================================================================== #
# 子树覆盖
# ====================================================================== #


def test_user_covers_own_agent_branch() -> None:
    parent = Scope(org="acme", user="alice")
    child = Scope(org="acme", user="alice", agent="assistant")
    assert scope_covers(parent, child)


def test_user_covers_own_agent_session_branch() -> None:
    parent = Scope(org="acme", user="alice")
    child = Scope(org="acme", user="alice", agent="assistant", session="s1")
    assert scope_covers(parent, child)


def test_agent_branch_does_not_cover_sibling_agent() -> None:
    parent = Scope(org="acme", user="alice", agent="assistant")
    child = Scope(org="acme", user="alice", agent="other")
    assert not scope_covers(parent, child)


def test_child_cannot_cover_parent() -> None:
    """覆盖是单向的：子分支拿不到父分支的范围。"""
    parent = Scope(org="acme", user="alice", agent="assistant")
    child = Scope(org="acme", user="alice")
    assert not scope_covers(parent, child)


def test_different_users_do_not_cover_each_other() -> None:
    assert not scope_covers(Scope(org="acme", user="alice"), Scope(org="acme", user="bob"))


# ====================================================================== #
# 空洞形状
# ====================================================================== #


def test_gapped_parent_does_not_cover() -> None:
    """``user=alice, agent="", session="s1"`` 跳过 agent 却限制 session。

    这种形状表达不成一棵连续子树。忽略空洞继续比会让一条写坏的 Grant 意外扩大
    覆盖面——它本想限制 session，结果变成了「alice 名下所有 agent 的 s1 会话」。
    """
    parent = Scope(org="acme", user="alice", session="s1")
    child = Scope(org="acme", user="alice", agent="assistant", session="s1")
    assert not scope_covers(parent, child)


# ====================================================================== #
# 主体路径
# ====================================================================== #


def test_agent_user_path_flips_the_nesting() -> None:
    """``agent_user`` 下 agent 是外层：一个 agent 服务多个 user 的平台形态。"""
    parent = Scope(org="acme", agent="bot")
    child = Scope(org="acme", user="alice", agent="bot")
    assert scope_covers(parent, child, principal_path=PrincipalPath.AGENT_USER)
    assert not scope_covers(parent, child, principal_path=PrincipalPath.USER_AGENT)


def test_user_agent_path_is_the_default() -> None:
    parent = Scope(org="acme", user="alice")
    child = Scope(org="acme", user="alice", agent="bot")
    assert scope_covers(parent, child)
    assert not scope_covers(parent, child, principal_path=PrincipalPath.AGENT_USER)


def test_principal_path_is_keyword_only() -> None:
    """位置传参会让「这次按哪种主体路径判定」在调用点看不出来。"""
    with pytest.raises(TypeError):
        scope_covers(  # type: ignore[misc]
            Scope(org="acme", user="alice"),
            Scope(org="acme", user="alice"),
            PrincipalPath.AGENT_USER,
        )
