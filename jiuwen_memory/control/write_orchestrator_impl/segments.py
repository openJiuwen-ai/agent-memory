# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入编排的纯函数：分组、锁段、切批（F10 决策 3、4、7）。

不含 I/O、不持有状态，便于单测与两条编排实现共用：

- **锁段**：按输入顺序把相邻、锁 key 相同的 infer 条目归为一段，锁的获取与释放以段
  为单位。``grouped`` 下每批就是一段；逐条执行时相邻同用户 infer 条目算一段。
- **切批**：只对需要合并抽取的段生效，按条数与 token 估算上限切分。
"""

from __future__ import annotations

from dataclasses import dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.lock import MutexScope
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem

#: 字符数 → token 的粗略估算系数。执行器不做精确分词：上限只是切批的保护值。
CHARS_PER_TOKEN = 4


def is_truthy(value: object) -> bool:
    return str(value or "").strip().lower() == "true"


def is_infer(item: BatchWriteItem) -> bool:
    """本条是否走「纯 infer」路径（middle / procedural 不参与合并、也不加锁）。"""
    metadata = item.system_metadata or {}
    if is_truthy(metadata.get("procedural")):
        return False
    if is_truthy(metadata.get("middle")):
        return False
    return is_truthy(metadata.get("infer"))


def lock_key(scope: Scope) -> tuple[str, str, str]:
    """锁的粒度与去重粒度一致：``(org, space, user)``，不含 agent / session。"""
    return scope.org, scope.space, scope.user


def lock_scope(scope: Scope) -> Scope:
    """锁只按三维定位；agent / session 置空，避免同一用户被拆成多把锁。"""
    return Scope(org=scope.org, space=scope.space, user=scope.user)


@dataclass(frozen=True)
class GroupKey:
    """分组键：载体 scope 五维 + ``stream_id``（G.FNM.05：用具名结构而非长元组）。"""

    org: str = ""
    space: str = ""
    user: str = ""
    agent: str = ""
    session: str = ""
    stream_id: str = ""


def group_key(item: BatchWriteItem) -> GroupKey:
    """分组键：``(载体 scope, stream_id)``。没传 stream_id 时整个 scope 算一组。"""
    scope = item.scope
    if scope is None:
        return GroupKey(stream_id=item.stream_id)
    return GroupKey(
        org=scope.org,
        space=scope.space,
        user=scope.user,
        agent=scope.agent,
        session=scope.session,
        stream_id=item.stream_id,
    )


@dataclass(frozen=True)
class Entry:
    """一条待写入条目及其在调用方列表中的位置。"""

    index: int
    item: BatchWriteItem


def split_runs(entries: list[Entry]) -> list[tuple[tuple[str, str, str] | None, list[Entry]]]:
    """按「同一锁 key 的连续 infer 段」切开，非 infer 条目段锁 key 为 ``None``。"""
    runs: list[tuple[tuple[str, str, str] | None, list[Entry]]] = []
    for entry in entries:
        scope = entry.item.scope
        key = lock_key(scope) if scope is not None and is_infer(entry.item) else None
        if runs and runs[-1][0] == key:
            runs[-1][1].append(entry)
        else:
            runs.append((key, [entry]))
    return runs


def estimate_tokens(item: BatchWriteItem) -> int:
    return max(1, len(item.content) // CHARS_PER_TOKEN)


def chunk_entries(
    entries: list[Entry],
    *,
    max_items: int,
    max_tokens: int,
) -> list[list[Entry]]:
    """按条数与 token 估算切批；任一项超限即切，保证每批至少一条。"""
    chunks: list[list[Entry]] = []
    current: list[Entry] = []
    tokens = 0
    for entry in entries:
        cost = estimate_tokens(entry.item)
        if current and (len(current) >= max_items or tokens + cost > max_tokens):
            chunks.append(current)
            current, tokens = [], 0
        current.append(entry)
        tokens += cost
    if current:
        chunks.append(current)
    return chunks


def validate_lock_scope(ctx, lock) -> None:
    """声明多实例却用进程内锁 → 装配失败（F10 决策 7）。

    校验的是编排器**实际使用**的那把锁，而不是装配根另建的实例：Factory 允许具名引用
    与内联实例，两处解析出的对象可以不同。
    """
    deployment = (getattr(ctx, "globals", None) or {}).get("deployment") or {}
    if not isinstance(deployment, dict):
        raise ValidationError("deployment must be a mapping")
    raw = deployment.get("multi_instance", False)
    if not isinstance(raw, bool):
        raise ValidationError("deployment.multi_instance must be bool")
    if raw and lock.mutex_scope is not MutexScope.CROSS_INSTANCE:
        raise ValidationError(
            "deployment.multi_instance=true 要求跨实例锁：请把 write.default.params.lock "
            "指向 redis 锁"
        )


def executor_from_config(config):
    """编排器取执行器：未声明时按缺省实现建匿名实例（单测/最小装配可用）。"""
    from jiuwen_memory.control.executor import BlockingExecutorProducer

    return BlockingExecutorProducer.dep(config, "executor", default="thread_pool")
