# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""UnitReader + 后置过滤——检索编排的「点读真源 + 复核」支撑件。

Option B 下，点读与复核从 Discloser 上移到 Retriever 阶段：Retriever 在融合截断后用
:class:`UnitReader` 把候选 id 物化为 :class:`~common.type_def.MemoryUnit`，再做三道
**后置过滤（纵深防御）**——索引侧若未/无法下推谓词时，这里读真源兜底正确性：

- :func:`passes`：lifecycle × as_of 有效性（拦 superseded 旧版本、合规遗忘、valid-time 回溯）；
- :func:`in_event_window`：event-time 窗（query 文本解析出的事件时间约束）；
- :func:`matches_filters`：调用方显式 ``filters``（tags/tier/… 谓词，支持 AND/OR/NOT 树）。

无论 Store 能否下推完整表达式，:func:`matches_filters` 都对完整 :data:`FilterExpr`
复核——真源复核保证最终返回精度；生产 Store 仍须在 limit 前完整下推，后置复核
无法找回已被 top-k 截断的真实命中。

序列化只在此处经 ``memory_codec`` 反序列化——「只在产出结果时 loads」的边界点。
"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.type_def import (
    FilterExpr,
    MemoryUnit,
    Scope,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.common.type_def.retrieval_filter import (
    in_event_window as _in_event_window,
)
from jiuwen_memory.common.type_def.retrieval_filter import (
    matches_retrieval_filters,
    passes_lifecycle,
)
from jiuwen_memory.common.type_def.retrieval_filter import (
    valid_at as _valid_at,
)
from jiuwen_memory.storage.kv import KVStore


def valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    """valid-time 判定：``as_of`` 是否落在 ``[t_valid, t_invalid)``。"""
    return _valid_at(unit, as_of)


def passes(unit: MemoryUnit, as_of: datetime | None, include_archived: bool = False) -> bool:
    """有效性过滤（lifecycle × as_of），决定候选是否应进入结果。

    - 当前态查询（``as_of is None``）：仅 ``ACTIVE`` 通过（``include_archived``
      时再放行 ``ARCHIVED``）——拦住仍留在索引里的 SUPERSEDED 旧版本（SUPERSEDE
      会同时写 ``t_invalid``，但 lifecycle 仍是独立的状态边界）；**再按此刻的
      valid-time 复核**——``t_invalid`` 已过但 lifecycle 仍是 ACTIVE 的记忆（例如
      尚未执行状态清扫）不属于「当前有效」。同时排除 ``t_valid`` 尚未到达的。
    - 时间点回溯（``as_of`` 给定）：FORGOTTEN 永不披露（合规遗忘）；其余按
      valid-time 区间判定，使「当时有效」的旧版本可被回溯。
    """
    return passes_lifecycle(unit, as_of, include_archived)


def in_event_window(
    unit: MemoryUnit, time_from: datetime | None, time_to: datetime | None
) -> bool:
    """event-time 窗过滤（半开区间 ``[time_from, time_to)``，对 ``t_event``）。

    **宽松兜底**：单元缺 ``t_event``、或 ``t_event`` 为 naive datetime（写入路径未做
    UTC 归一化，issue #91）时均不据此丢弃——前者 t_event 可能尚未 populated，后者
    与 aware 窗口比较会抛 TypeError；索引侧才是主过滤口，本函数只剔除「明确落窗外」的，
    避免 over-drop 伤召回。
    """
    return _in_event_window(unit, time_from, time_to)


def matches_filters(unit: MemoryUnit, expr: FilterExpr | None) -> bool:
    """调用方显式 ``filters`` 的后置评估（完整 AND/OR/NOT 表达式）。

    内核只接受规范化后的 :data:`FilterExpr`（旧式 list / dict 由查询对象边界的
    ``normalize`` 收口，内部链路不再出现旧列表分支）。字段投影、树遍历和叶子比较
    统一委托 :func:`~common.type_def.matches_memory_unit`，与 KV list 使用同一语义。
    ``None`` → 全通过。
    """
    return matches_retrieval_filters(unit, expr)


class UnitReader:
    """按 id 点读真源记忆单元（正排）。

    点读在**查询 scope** 内进行——召回与点读同属一次 ``retrieve(scope, …)``，候选必落该
    scope，故直接在该 scope 点读，不再跨 scope 扫描。（若日后支持子树通配召回、命中跨
    物理 scope，则改由命中 payload 回传 scope，见契约 spec §4.1；本接口不变。）
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    @property
    def kv(self) -> KVStore:
        """供统一 Storage 装配复用同一真源实例。"""
        return self._kv

    def load(self, scope: Scope, unit_ids: list[str]) -> dict[str, MemoryUnit]:
        """
        在 ``scope`` 内批量点读，返回 ``unit_id → MemoryUnit``；缺失的 id 省略
        （索引↔真源短暂不一致）。

        重复 ``unit_id`` 的去重在此处（load）完成——先去重再一次性 ``mget`` 召回
        真源字节（位置对应、重复 key 各下标独立返回），省去逐条 ``get`` 的 kv 接口
        往返；去重不下沉到 ``mget``。``mget`` 任一 key 缺失即报 ``NotFoundError``
        （与 ``get`` 一致），故批量点读的「缺失省略」兜底由 load 承担：捕获缺失时
        回退逐条 ``get``，仅跳过确实不存在的 id。
        """
        # 去重保序：mget 按位置返回，传入去重后的 key 避免重复点读/重复反序列化。
        unique = list(dict.fromkeys(unit_ids))
        keys = [memory_key(uid) for uid in unique]
        try:
            return {uid: loads(data) for uid, data in zip(unique, self._kv.mget(scope, keys))}
        except NotFoundError:
            # 索引↔真源短暂不一致（个别 id 尚未落盘）：退回逐条，仅跳过缺失。
            return self._load_missing_fallback(scope, unique)

    def _load_missing_fallback(
        self, scope: Scope, unique: list[str]
    ) -> dict[str, MemoryUnit]:
        """mget 报缺失时的逐条兜底：逐个 get，缺失的 id 跳过（不报错）。"""
        out: dict[str, MemoryUnit] = {}
        for uid in unique:
            try:
                out[uid] = loads(self._kv.get(scope, memory_key(uid)))
            except NotFoundError:
                continue
        return out
