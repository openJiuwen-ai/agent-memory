# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""入口到轴与动作的映射表的完整性与一致性断言（F07「入口到轴与动作的映射」）。

映射表与归属主体档两级清单的分工由三条断言固定，任一不成立即两侧的分工被改坏；
另有一条覆盖完整性断言，防止新增入口漏配后静默绕开空间级判定。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from jiuwen_memory.common.security.space_roles import (
    CONTENT_ACTIONS,
    CONTENT_ACTIONS_OWN,
    ENTRY_RULES,
    GOVERNANCE_ACTIONS,
    OWNER_ENTRY_COVERS,
    OWNER_ENTRY_SAME_DIMS,
    SpaceAction,
    SpaceAxis,
    SpaceContentRole,
    SpaceGovernanceRole,
)

pytestmark = pytest.mark.unit

_API_IMPL = pathlib.Path(__file__).parents[4] / (
    "jiuwen_memory/api/memory_api_impl/local_memory_api.py"
)


def _authorize_entry_names() -> set[str]:
    """从鉴权点的全部调用点取出入口名实参。

    按 AST 取而不是文本匹配：入口名是第四个位置参数，多行调用的文本匹配会漏。
    两个鉴权方法都要取：``search`` 只经 ``_authorize_with_context`` 调用，只取前者
    会使该入口在两条完整性断言中都不可见。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(_API_IMPL.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("_authorize", "_authorize_with_context"):
            continue
        if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
            names.add(node.args[3].value)
        for keyword in node.keywords:
            if keyword.arg == "audit_action" and isinstance(keyword.value, ast.Constant):
                names.add(keyword.value.value)
    return names


def test_every_authorize_call_site_has_an_entry_rule() -> None:
    """覆盖完整性：鉴权点用到的入口名都能在映射表中查到。

    漏配一个入口的后果是它绕开某条轴的判定，而散落在各方法里的形态无法一眼核对。
    """
    missing = _authorize_entry_names() - set(ENTRY_RULES)
    assert not missing, f"入口未登记到 ENTRY_RULES: {sorted(missing)}"


def test_owner_entry_grades_do_not_overlap() -> None:
    """两级清单不重叠：逐维相同者自然满足覆盖，重叠即判据二义。"""
    assert not (OWNER_ENTRY_SAME_DIMS & OWNER_ENTRY_COVERS)


def test_owner_entry_grades_are_all_in_the_mapping_table() -> None:
    """清单里出现映射表没有的入口名时该项静默失效。"""
    unknown = (OWNER_ENTRY_SAME_DIMS | OWNER_ENTRY_COVERS) - set(ENTRY_RULES)
    assert not unknown, f"清单入口不在 ENTRY_RULES: {sorted(unknown)}"


def test_policy_trimming_entries_share_the_second_grade() -> None:
    """``get_space`` / ``list_spaces`` / ``get_space_policy`` 同处第二级。

    保的是策略裁剪的判据与可读判据不脱钩：只把 ``get_space_policy`` 归治理轴而不裁剪
    前两者的返回值，调用方改调它们即可照样读走策略。
    """
    for entry in ("get_space", "list_spaces", "get_space_policy"):
        assert entry in OWNER_ENTRY_COVERS, entry


def test_org_level_entries_take_org_axis() -> None:
    """组织级入口走角色闸门，不落两轴求值。"""
    for entry in (
        "create_space",
        "audit",
        "verify_audit",
        "admin_get",
        "admin_all",
        "admin_set",
    ):
        assert ENTRY_RULES[entry].axis is SpaceAxis.ORG, entry


def test_governance_entries_never_take_the_content_axis() -> None:
    """两级清单中的治理入口不得配成内容轴。

    ``export_space`` 是唯一的例外，它取内容轴读动作、按归属主体档第一级裁决——
    第 7 步对它另设排除，见 space_decision 的说明。
    """
    for entry in OWNER_ENTRY_SAME_DIMS:
        if entry == "export_space":
            assert ENTRY_RULES[entry].axis is SpaceAxis.CONTENT
            continue
        assert ENTRY_RULES[entry].axis is SpaceAxis.GOVERNANCE, entry


def test_content_matrix_is_monotonic_by_rank() -> None:
    """内容轴档位越高动作集合越大，不出现高档缺低档动作。"""
    order = [
        SpaceContentRole.NONE,
        SpaceContentRole.VIEWER,
        SpaceContentRole.CONTRIBUTOR,
        SpaceContentRole.EDITOR,
    ]
    for lower, higher in zip(order, order[1:]):
        assert CONTENT_ACTIONS[lower] <= CONTENT_ACTIONS[higher], (lower, higher)


def test_governance_matrix_is_monotonic_by_rank() -> None:
    order = [SpaceGovernanceRole.NONE, SpaceGovernanceRole.MANAGER, SpaceGovernanceRole.OWNER]
    for lower, higher in zip(order, order[1:]):
        assert GOVERNANCE_ACTIONS[lower] <= GOVERNANCE_ACTIONS[higher], (lower, higher)


def test_own_actions_only_extend_contributor() -> None:
    """限「本人所写」的附加动作只有 contributor 有。

    editor 对空间内任一条目都可改删，附加集合是空集而非重复项；给 editor 配上会使
    「本人所写」这条判据在 editor 档上看起来生效，实际无差别。
    """
    assert CONTENT_ACTIONS_OWN[SpaceContentRole.CONTRIBUTOR] == frozenset(
        {SpaceAction.UPDATE, SpaceAction.DELETE}
    )
    for role in (SpaceContentRole.NONE, SpaceContentRole.VIEWER, SpaceContentRole.EDITOR):
        assert not CONTENT_ACTIONS_OWN[role], role


def test_content_axis_never_grants_organisation_level_actions() -> None:
    """四个组织级动作不出现在任一轴的矩阵里：它们由角色闸门终局裁决。"""
    org_actions = {
        SpaceAction.MANAGE_SPACE,
        SpaceAction.READ_AUDIT,
        SpaceAction.VERIFY_AUDIT,
        SpaceAction.ADMINISTER_SYSTEM,
    }
    for actions in list(CONTENT_ACTIONS.values()) + list(GOVERNANCE_ACTIONS.values()):
        assert not (actions & org_actions)


def test_the_table_has_no_entry_that_is_never_used() -> None:
    """反向完整性：映射表里没有鉴权点从不使用的入口名。

    不可达的配置项本身无功能影响，但它会让读者以为存在该路径；更要紧的是本断言与上一条
    合起来使入口名的改动必然被发现——只改调用点不改表，新名字落到「未登记即不做空间级
    判定」，那是一条静默的越权路径。
    """
    unused = set(ENTRY_RULES) - _authorize_entry_names()
    assert not unused, f"映射表中的入口名无调用点: {sorted(unused)}"


def test_only_batch_entries_close_the_own_actions_set() -> None:
    """「本人所写」附加集合只对目标可归属到单一作者的入口开放。

    演进与两个任务入口作用于整个空间、条目作者各不相同，且没有第二段鉴权来收边界；
    对它们并入附加集合等于按最宽的一条放行整批。
    """
    closed = {name for name, rule in ENTRY_RULES.items() if not rule.own_actions_apply}
    assert closed == {"evolve", "job_status", "job_cancel"}
