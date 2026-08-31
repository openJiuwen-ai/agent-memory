"""Scope 覆盖规则（F05 §Authorization 决策顺序第 4 步）。

从 ``control.permission_impl.sqlite_permission_manager._owner_scope_covers`` 迁出。
独立成模块的理由：owner 判定与 Grant 匹配用的是**同一套**覆盖规则（「grantee 覆盖
actor」且「grantor 覆盖 target」），把它留在某个 Authorizer 实现内部，第二个实现就
会抄一份，两份迟早分叉——而这条规则分叉的表现形式是越权。

与旧实现的一处**行为差异**：不再有「``parent == Scope()`` 即覆盖一切」的通配分支。
旧实现靠它同时表达两件事——platform admin 与 grant 行的宽松匹配——而 F05 §授权
不变量 1 要求 ROOT 只由 role 表达。特权判定移到 Authorizer 的角色闸门，这里只剩
纯粹的「父 scope 是否包含子 scope」。
"""

from __future__ import annotations

from enum import Enum

from jiuwen_memory.common.type_def.scope import Scope


class PrincipalPath(str, Enum):
    """主体维度的嵌套顺序。由 space policy 决定，不由请求决定。

    ``USER_AGENT``：user 是外层，一个 user 名下可有多个 agent（个人助理形态）。
    ``AGENT_USER``：agent 是外层，一个 agent 服务多个 user（平台 bot 形态）。

    同名字符串值与 ``control.types.PrincipalPath`` 对齐，便于 PEP 从 space policy
    直接转换。
    """

    USER_AGENT = "user_agent"
    AGENT_USER = "agent_user"


def _dimension_order(path: PrincipalPath) -> tuple[str, str, str]:
    if path is PrincipalPath.AGENT_USER:
        return ("agent", "user", "session")
    return ("user", "agent", "session")


def scope_covers(
    parent: Scope,
    child: Scope,
    *,
    principal_path: PrincipalPath = PrincipalPath.USER_AGENT,
) -> bool:
    """``parent`` 是否覆盖 ``child``（即 child 在 parent 的所有者范围内）。

    规则，按维度从外到内：

    - ``org`` 与 ``space`` 必须完全相等——两者都是硬边界，不存在「父 org 覆盖子 org」；
    - 主体维度按 ``principal_path`` 决定顺序，逐层比较；
    - parent 在某维**留空**表示「该维及更内层不限制」，但更内层必须也全空：
      ``user=alice, agent="", session="s1"`` 不是一个合法的父范围——它跳过了 agent
      却又限制 session，无法表达成一棵连续的子树。这种形状一律不覆盖，而不是忽略
      空洞继续比——忽略空洞会让写坏的 Grant 意外扩大覆盖面。

    ``principal_path`` 是 keyword-only：它改变的是**判定语义本身**，位置传参会让
    「这次按哪种主体路径判定」在调用点看不出来。
    """
    if parent.org != child.org or parent.space != child.space:
        return False

    order = _dimension_order(principal_path)
    primary = order[0]
    if getattr(parent, primary) != getattr(child, primary):
        # 最外层主体维必须精确相等：留空的 parent 覆盖不了具名的 child，那会让
        # 一条「不限 user」的记录横扫整个 space。
        return False

    for index, dim in enumerate(order[1:], start=1):
        parent_value = getattr(parent, dim)
        if parent_value:
            if parent_value != getattr(child, dim):
                return False
            continue
        # parent 在本维留空 → 更内层也必须全空，否则是上面说的「空洞」形状。
        next_index = index + 1
        return not any(getattr(parent, later) for later in order[next_index:])
    return True
