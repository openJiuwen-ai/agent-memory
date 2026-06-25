"""UnitReader + 后置过滤——检索编排的「点读真源 + 复核」支撑件。

Option B 下，点读与复核从 Discloser 上移到 Retriever 阶段：Retriever 在融合截断后用
:class:`UnitReader` 把候选 id 物化为 :class:`~common.type_def.MemoryUnit`，再做三道
**后置过滤（纵深防御）**——索引侧若未/无法下推谓词时，这里读真源兜底正确性：

- :func:`passes`：lifecycle × as_of 有效性（拦 superseded 旧版本、合规遗忘、valid-time 回溯）；
- :func:`in_event_window`：event-time 窗（query 文本解析出的事件时间约束）；
- :func:`matches_filters`：调用方显式 ``filters``（tags/tier/… 标量谓词）。

序列化只在此处经 ``memory_codec`` 反序列化——「只在产出结果时 loads」的边界点。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

from common.errors import NotFoundError
from common.type_def import FilterClause, FilterOp, LifecycleState, MemoryUnit, Scope
from common.type_def.memory_codec import loads
from storage.kv import KVStore


def valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    """valid-time 判定：``as_of`` 是否落在 ``[t_valid, t_invalid)``。"""
    t = unit.temporal
    if t.t_valid is not None and as_of < t.t_valid:
        return False
    if t.t_invalid is not None and as_of >= t.t_invalid:
        return False
    return True


def passes(unit: MemoryUnit, as_of: Optional[datetime], include_archived: bool = False) -> bool:
    """有效性过滤（lifecycle × as_of），决定候选是否应进入结果。

    - 当前态查询（``as_of is None``）：仅 ``ACTIVE`` 通过（``include_archived``
      时再放行 ``ARCHIVED``）——拦住仍留在索引里的 SUPERSEDED 旧版本（SUPERSEDE
      只标状态、未设 t_invalid，故必须按 lifecycle 拦而非仅按时间）。
    - 时间点回溯（``as_of`` 给定）：FORGOTTEN 永不披露（合规遗忘）；其余按
      valid-time 区间判定，使「当时有效」的旧版本可被回溯。
    """
    if as_of is None:
        allowed = {LifecycleState.ACTIVE}
        if include_archived:
            allowed.add(LifecycleState.ARCHIVED)
        return unit.lifecycle in allowed
    if unit.lifecycle == LifecycleState.FORGOTTEN:
        return False
    return valid_at(unit, as_of)


def in_event_window(
    unit: MemoryUnit, time_from: Optional[datetime], time_to: Optional[datetime]
) -> bool:
    """event-time 窗过滤（半开区间 ``[time_from, time_to)``，对 ``t_event``）。

    **宽松兜底**：单元缺 ``t_event`` 时不据此丢弃（t_event 可能尚未 populated，且索引
    侧才是主过滤口；本函数只剔除「明确落窗外」的，避免 over-drop 伤召回）。
    """
    if time_from is None and time_to is None:
        return True
    te = unit.temporal.t_event
    if te is None:
        return True
    if time_from is not None and te < time_from:
        return False
    if time_to is not None and te >= time_to:
        return False
    return True


def matches_filters(unit: MemoryUnit, clauses: Sequence[FilterClause]) -> bool:
    """调用方显式 ``filters`` 的后置评估（AND 组合）。索引侧未下推时在此兜底。"""
    return all(_matches(unit, c) for c in clauses)


def _epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else int(dt.timestamp() * 1000)


def _field_value(unit: MemoryUnit, field: str):
    """把 FilterClause.field 映射到 MemoryUnit 上的可过滤字段（契约 §2 字段表）。"""
    if field == "tags":
        return unit.tags
    if field == "tier":
        return unit.tier.value
    if field == "source":
        return unit.source.value
    if field == "lifecycle":
        return unit.lifecycle.value
    if field in ("unit_id", "id"):
        return unit.id
    if field == "t_event":
        return _epoch_ms(unit.temporal.t_event)
    if field == "t_valid":
        return _epoch_ms(unit.temporal.t_valid)
    if field == "t_invalid":
        return _epoch_ms(unit.temporal.t_invalid)
    return unit.metadata.get(field)  # 其余按 metadata 键取


def _matches(unit: MemoryUnit, clause: FilterClause) -> bool:
    val = _field_value(unit, clause.field)
    op, target = clause.op, clause.value

    if isinstance(val, (list, tuple, set)):  # 集合字段（如 tags）
        members = set(val)
        if op in (FilterOp.CONTAINS, FilterOp.EQ):
            return target in members
        if op == FilterOp.NE:
            return target not in members
        if op == FilterOp.IN:
            return bool(members & set(target or []))
        if op == FilterOp.NOT_IN:
            return not (members & set(target or []))
        return True  # 数值比较对集合无意义 → 不约束

    if val is None:  # 缺值：仅否定类算子成立（避免 over-drop 非该类约束）
        return op in (FilterOp.NE, FilterOp.NOT_IN)

    if op == FilterOp.EQ:
        return val == target
    if op == FilterOp.NE:
        return val != target
    if op == FilterOp.IN:
        return val in (target or [])
    if op == FilterOp.NOT_IN:
        return val not in (target or [])
    if op == FilterOp.CONTAINS:
        return str(target) in str(val)
    if op == FilterOp.GT:
        return val > target
    if op == FilterOp.GTE:
        return val >= target
    if op == FilterOp.LT:
        return val < target
    if op == FilterOp.LTE:
        return val <= target
    return True


class UnitReader:
    """按 id 点读真源记忆单元（正排）。

    点读在**查询 scope** 内进行——召回与点读同属一次 ``retrieve(scope, …)``，候选必落该
    scope，故直接在该 scope 点读，不再跨 scope 扫描。（若日后支持子树通配召回、命中跨
    物理 scope，则改由命中 payload 回传 scope，见契约 spec §4.1；本接口不变。）
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def load(self, scope: Scope, unit_ids: List[str]) -> Dict[str, MemoryUnit]:
        """
        在 ``scope`` 内批量点读，返回 ``unit_id → MemoryUnit``；缺失的 id 省略
        （索引↔真源短暂不一致）。
        """
        out: Dict[str, MemoryUnit] = {}
        for uid in unit_ids:
            if uid in out:
                continue
            try:
                out[uid] = loads(self._kv.get(scope, uid))
            except NotFoundError:
                continue
        return out
