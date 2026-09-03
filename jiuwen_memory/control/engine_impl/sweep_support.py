# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``sweep_expired`` 的共享编排逻辑（InMemoryEngine / CloudEngine 复用）。

语义来自 C-03：LifecycleManager 只做纯计算（返回
:class:`~control.lifecycle.SweepTransition`），Engine 编排执行——

1. 按 (scope, 目标态) 分组；
2. FORGOTTEN 组**先** ``IndexBuilder.remove(mode=SOFT)`` 移出检索索引，
   成功后再回写真源 lifecycle；ARCHIVED 组只回写（``include_archived``
   召回与 ``as_of`` 回溯仍需检索索引，不删）；
3. 任一步失败的组保持原状态、计入 ``failed``——单元未流转，下轮 sweep
   重新发现（remove 幂等），重试自愈，不静默当成功。

顺序不变量（先删索引、后回写真源）是失败可重试的前提：remove 失败时单元
仍是 ACTIVE，下轮 sweep 重新选中；反序会在 remove 失败时留下真源已
FORGOTTEN、索引未清且永不再被 sweep 选中的脏条目（``_sweep_target`` 对
FORGOTTEN 返回 None）。
"""

from __future__ import annotations

from collections.abc import Callable

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope
from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition
from jiuwen_memory.control.types import SweepResult

logger = get_logger(__name__)

_GroupKey = tuple[str, str, str, str, str, str]


def _group_key(transition: SweepTransition) -> _GroupKey:
    scope = transition.scope
    return (
        scope.org,
        scope.space,
        scope.user,
        scope.agent,
        scope.session,
        transition.to_state.value,
    )


def run_sweep(
    transitions: list[SweepTransition],
    lifecycle: LifecycleManager,
    remove_from_index: Callable[[list[MemoryUnit]], None],
) -> SweepResult:
    """执行一批 sweep transition：分组后先删索引（仅 FORGOTTEN）、再回写真源。

    ``remove_from_index`` 由调用方注入（InMemoryEngine 直调共享 IndexBuilder，
    CloudEngine 按各 pipeline 的 builder 分组删除），须表达 ``remove(mode=SOFT)``。
    """
    groups: dict[_GroupKey, tuple[Scope, LifecycleState, list[SweepTransition]]] = {}
    for transition in transitions:
        _, _, group = groups.setdefault(
            _group_key(transition), (transition.scope, transition.to_state, [])
        )
        group.append(transition)

    result = SweepResult()
    for scope, target, group in groups.values():
        try:
            if target is LifecycleState.FORGOTTEN:
                remove_from_index([t.unit for t in group])
            lifecycle.transition(scope, [t.unit_id for t in group], target)
        except Exception as exc:  # 治理清扫逐组容错：失败组留待下轮重试，不中断其他组
            result.failed.extend(t.unit_id for t in group)
            logger.warning(
                "Engine.sweep_expired group failed (kept for retry): scope=%s target=%s"
                " count=%d error=%s",
                scope,
                target.value,
                len(group),
                exc,
            )
            continue
        result.swept.extend(t.unit_id for t in group)
    result.swept.sort()
    result.failed.sort()
    logger.info(
        "Engine.sweep_expired: swept=%d failed=%d",
        len(result.swept),
        len(result.failed),
    )
    return result
