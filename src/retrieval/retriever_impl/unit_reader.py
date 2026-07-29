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

from datetime import datetime, timezone

from common.errors import NotFoundError
from common.type_def import (
    T_INVALID_OPEN,
    FilterClause,
    FilterExpr,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    Scope,
    evaluate,
    filter_field_metadata_key,
    memory_key,
)
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
    if as_of is None:
        allowed = {LifecycleState.ACTIVE}
        if include_archived:
            allowed.add(LifecycleState.ARCHIVED)
        if unit.lifecycle not in allowed:
            return False
        return valid_at(unit, datetime.now(timezone.utc))
    if unit.lifecycle == LifecycleState.FORGOTTEN:
        return False
    return valid_at(unit, as_of)


def in_event_window(
    unit: MemoryUnit, time_from: datetime | None, time_to: datetime | None
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


def matches_filters(unit: MemoryUnit, expr: FilterExpr | None) -> bool:
    """调用方显式 ``filters`` 的后置评估（完整 AND/OR/NOT 表达式）。

    内核只接受规范化后的 :data:`FilterExpr`（旧式 list / dict 由查询对象边界的
    ``normalize`` 收口，内部链路不再出现旧列表分支）。树遍历/短路复用
    :func:`~common.type_def.evaluate`，叶子比较沿用 :func:`_matches`（字段映射、
    集合字段、缺值语义、时间 epoch 转换保持不变）。``None`` → 全通过。
    """
    return evaluate(expr, lambda clause: _matches(unit, clause))


def _epoch_ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


def _field_value(unit: MemoryUnit, field: str):
    """把 FilterClause.field 映射到 MemoryUnit 上的可过滤字段（契约 §2 字段表）。

    系统字段（tags/tier/lifecycle/时间等）在下方提前 return；其余落到 metadata，
    ``metadata`` 值为 JSON 标量原生类型，直接返回参与比较——真源与索引写入的是同
    一个对象，两侧判定不会分叉。
    """
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
        # 与索引投影同值：空（永久有效）在索引里落 T_INVALID_OPEN，此处若返回 None，
        # 调用方显式的 t_invalid 谓词会被下推放行、被本函数按缺值砍掉。本函数与
        # construction 的 _index_metadata 是同一层投影，须共用哨兵约定。
        # valid_at 不走这里，仍按真源 None 判「永久有效」。
        t = _epoch_ms(unit.temporal.t_invalid)
        return T_INVALID_OPEN if t is None else t
    return unit.metadata.get(filter_field_metadata_key(field))  # 其余按 metadata 键取


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
        # 范围算子对集合无意义：判否，与下方标量不可比时的 TypeError 分支同答案。
        # 放行会让该谓词失效——UnitReader 是正确性边界，不可判定的组合不能当作不约束。
        return False

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
        # 标量上退化为等值，与 ES 编译器一致（CONTAINS 与 EQ 同走 term）。子串匹配会
        # 与两个后端的下推语义分叉：图通道不下推、只经本函数复核，同一查询将因命中
        # 通道不同而给出不同结果。数组成员包含由上方集合分支承担。
        return val == target
    try:
        if op == FilterOp.GT:
            return val > target
        if op == FilterOp.GTE:
            return val >= target
        if op == FilterOp.LT:
            return val < target
        if op == FilterOp.LTE:
            return val <= target
    except TypeError:
        # 真源值与谓词类型不可比（该 key 存的是字符串、谓词是数值等）：判否而非让
        # TypeError 中断整次检索。与 Milvus JSON 字段同语义——类型严格、不隐式转换，
        # 不匹配的记录跳过而非报错。
        return False
    return True


class UnitReader:
    """按 id 点读真源记忆单元（正排）。

    点读在**查询 scope** 内进行——召回与点读同属一次 ``retrieve(scope, …)``，候选必落该
    scope，故直接在该 scope 点读，不再跨 scope 扫描。（若日后支持子树通配召回、命中跨
    物理 scope，则改由命中 payload 回传 scope，见契约 spec §4.1；本接口不变。）
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def load(self, scope: Scope, unit_ids: list[str]) -> dict[str, MemoryUnit]:
        """
        在 ``scope`` 内批量点读，返回 ``unit_id → MemoryUnit``；缺失的 id 省略
        （索引↔真源短暂不一致）。
        """
        out: dict[str, MemoryUnit] = {}
        for uid in unit_ids:
            if uid in out:
                continue
            try:
                out[uid] = loads(self._kv.get(scope, memory_key(uid)))
            except NotFoundError:
                continue
        return out
