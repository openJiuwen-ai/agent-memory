"""检索/前置过滤的结构化谓词（跨层共用）。

替代「``dict[str, str]`` 平铺等值」的弱表达：单条 :class:`FilterClause`
支持等值、集合命中（in）、数值/时间范围（gt/lt 等）、列表字段包含
（contains）；多条子句默认 AND（取交）组合。接口层 / 检索层 / 存储层
共用同一类型，既消除类型断层，也不丢失表达力（集合、范围都能表达）。
scope 不走这里——它是各记录/查询上的专用字段（原生隔离），filters 仅
承载 scope 之外的额外谓词。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FilterOp(str, Enum):
    """过滤算子。"""

    EQ = "eq"  # 等于
    NE = "ne"  # 不等于
    IN = "in"  # 属于集合（value 为 list）
    NOT_IN = "not_in"  # 不属于集合（value 为 list）
    GT = "gt"  # 大于
    GTE = "gte"  # 大于等于
    LT = "lt"  # 小于
    LTE = "lte"  # 小于等于
    CONTAINS = "contains"  # 列表/集合型字段包含某元素（如 tags 含某标签）


@dataclass
class FilterClause:
    """单条过滤谓词：字段 ``field`` 经算子 ``op`` 与 ``value`` 比较。

    多条子句由调用方放进列表，语义为 AND（取交）；字段内的「或」用
    ``IN`` 表达。``value`` 类型随算子而定：``IN``/``NOT_IN`` 为 list，
    ``EQ``/范围类为标量，``CONTAINS`` 为单个元素。
    """

    field: str  # 字段名：标签维度 / 元数据 key / 标量字段名
    op: FilterOp = FilterOp.EQ  # 比较算子
    value: Any = None  # 比较值（语义随 op 而定）
