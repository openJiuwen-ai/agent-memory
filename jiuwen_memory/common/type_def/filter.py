"""检索/前置过滤的结构化谓词（跨层共用）。

替代「``dict[str, str]`` 平铺等值」的弱表达：单条 :class:`FilterClause`
支持等值、集合命中（in）、数值/时间范围（gt/lt 等）、列表字段包含
（contains）；:class:`FilterGroup` 再把叶子按 ``AND`` / ``OR`` / ``NOT``
组织成树，表达 ``(a OR b) AND NOT c`` 一类逻辑。:data:`FilterExpr` 统一承载
叶子或逻辑树。接口层 / 检索层 / 存储层共用同一类型：业务层只构造这个后端
无关的结构化对象，各存储后端各自把它翻译成本地 DSL（Milvus 表达式串 / ES
bool DSL / 内存递归求值）。生产 Store 在 limit 前完整下推以保证 top-k 完整性，
UnitReader 点读真源递归复核以保证最终返回精度。

边界工具：:func:`normalize` 把旧式 ``list``、单叶子、dict DSL 统一为
:data:`FilterExpr`；:func:`from_dict` 解析 HTTP/SDK 的 dict DSL；
:func:`extract_required_equality` 供路由从表达式安全取唯一等值。

scope 不走这里——它是各记录/查询上的专用字段（原生隔离），filters 仅承载
scope 之外的额外谓词；:func:`validate` 会拒绝 scope 字段混入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any, Callable, Iterator

from jiuwen_memory.common.errors import ValidationError


class FilterOp(str, Enum):
    """过滤算子。"""

    EQ = "eq"  # 标量等于；数组字段不按成员命中
    NE = "ne"  # EQ 的逻辑否定
    IN = "in"  # 标量属于查询集合（value 为 list）
    NOT_IN = "not_in"  # IN 的逻辑否定
    GT = "gt"  # 大于
    GTE = "gte"  # 大于等于
    LT = "lt"  # 小于
    LTE = "lte"  # 小于等于
    # 数组成员包含（如 tags 含某标签）。语义仅限「数组含该元素」，不表示字符串子串——
    # 生产后端都按成员包含编译；标量既不做等值退化，也不做字符串子串匹配。
    CONTAINS = "contains"


class FilterLogic(str, Enum):
    """逻辑节点算子：组织子表达式的 ``AND`` / ``OR`` / ``NOT``。"""

    AND = "and"  # 所有 children 都满足
    OR = "or"  # 至少一个 child 满足
    NOT = "not"  # 只接受一个 child，取反


@dataclass
class FilterClause:
    """单条过滤谓词：字段 ``field`` 经算子 ``op`` 与 ``value`` 比较。

    唯一的字段比较节点（叶子）。``value`` 类型随算子而定：``IN``/``NOT_IN``
    为非空同类型标量 list/tuple，``EQ``/``NE``/``CONTAINS`` 为标量，范围算子
    为有限数值。字段内的「或」用 ``IN`` 表达；跨字段逻辑交给
    :class:`FilterGroup`。
    """

    field: str  # 字段名：标签维度 / 元数据 key / 标量字段名
    op: FilterOp = FilterOp.EQ  # 比较算子
    value: Any = None  # 比较值（语义随 op 而定）


@dataclass
class FilterGroup:
    """逻辑节点：按 ``logic`` 组合一组子表达式（叶子或嵌套的 group）。

    ``AND`` / ``OR`` 需至少一个 child（空组非法）；``NOT`` 恰好一个 child。
    合法性由 :func:`validate` 递归校验。
    """

    logic: FilterLogic  # 逻辑算子
    children: list[FilterExpr] = field(default_factory=list)  # 子表达式（叶子或 group）


# 叶子或逻辑树的统一承载类型：检索/存储契约以此为准（None 表示无过滤）。
FilterExpr = FilterClause | FilterGroup

# scope 是隔离轴、走独立入参，不得混入 filters（见模块 docstring）。
_SCOPE_FIELDS = frozenset({"scope", "tenant", "org", "space", "user", "agent", "session"})
_SET_OPS = frozenset({FilterOp.IN, FilterOp.NOT_IN})
_RANGE_OPS = frozenset({FilterOp.GT, FilterOp.GTE, FilterOp.LT, FilterOp.LTE})
_LOGIC_KEYS = {"AND": FilterLogic.AND, "OR": FilterLogic.OR, "NOT": FilterLogic.NOT}
_USER_METADATA_PREFIX = "user_metadata."
_SYSTEM_METADATA_PREFIX = "system_metadata."
_BUILTIN_FIELDS = frozenset(
    {
        "tags",
        "tier",
        "source",
        "lifecycle",
        "unit_id",
        "id",
        "t_event",
        "t_valid",
        "t_invalid",
        "t_message",
        "content_layer",
        "seq",
    }
)

# memory_type 的过滤字段采用唯一规范名；裸 ``memory_type`` 仅作为旧输入别名在 normalize
# 边界转换，权限路由、Pipeline、Store 和 UnitReader 内部一律只见该规范名。
MEMORY_TYPE_FILTER_FIELD = "system_metadata.memory_type"


def canonical_filter_field(name: str) -> str:
    """把兼容字段名转换为内核规范名。"""
    if name in _SCOPE_FIELDS or name.startswith("scope_"):
        return name
    if name == "memory_type":
        return MEMORY_TYPE_FILTER_FIELD
    if "." not in name and name not in _BUILTIN_FIELDS:
        return f"user_metadata.{name}"
    return name


def filter_field_metadata_key(name: str) -> str:
    """返回索引中的规范字段名。"""
    return name


def _check_field(name: str) -> None:
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValidationError("filter 字段名必须是非空字符串")
    if name.startswith("metadata.") or name == "metadata":
        raise ValidationError("metadata 命名空间已移除；请使用 user_metadata.<key>")
    if name in {_USER_METADATA_PREFIX, _SYSTEM_METADATA_PREFIX}:
        raise ValidationError("metadata filter 字段必须包含 key")
    if "." in name and not name.startswith(
        (_USER_METADATA_PREFIX, _SYSTEM_METADATA_PREFIX)
    ):
        raise ValidationError(f"未知的 filter 字段命名空间：{name!r}")
    if name in _SCOPE_FIELDS or name.startswith("scope_"):
        raise ValidationError(f"scope 字段不得出现在 filters：{name!r}")


def _scalar_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "str"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float) and isfinite(value):
        return "number"
    return None


def _normalize_value(op: FilterOp, value: Any) -> Any:
    if op in _SET_OPS:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValidationError(f"{op.value} 的 value 必须是非空 list/tuple：{value!r}")
        normalized = list(value)
        kinds = {_scalar_kind(item) for item in normalized}
        if None in kinds:
            raise ValidationError(f"{op.value} 的元素必须是可序列化标量：{value!r}")
        if len(kinds) != 1:
            raise ValidationError(f"{op.value} 的元素类型类别必须一致：{value!r}")
        return normalized
    if op in _RANGE_OPS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{op.value} 的 value 必须是有限数值：{value!r}")
        if isinstance(value, float) and not isfinite(value):
            raise ValidationError(f"{op.value} 的 value 必须是有限数值：{value!r}")
        return value
    if _scalar_kind(value) is None:
        raise ValidationError(f"{op.value} 的 value 必须是可序列化标量：{value!r}")
    return value


def _normalize_clause(clause: FilterClause) -> FilterClause:
    _check_field(clause.field)
    if not isinstance(clause.op, FilterOp):
        raise ValidationError(f"未知过滤算子：{clause.op!r}")
    return FilterClause(
        field=canonical_filter_field(clause.field),
        op=clause.op,
        value=_normalize_value(clause.op, clause.value),
    )


def _normalize_expr(expr: FilterExpr) -> FilterExpr:
    if isinstance(expr, FilterClause):
        return _normalize_clause(expr)
    if not isinstance(expr, FilterGroup):
        raise ValidationError(f"非法的过滤表达式类型：{type(expr).__name__}")
    if not isinstance(expr.logic, FilterLogic):
        raise ValidationError(f"未知逻辑算子：{expr.logic!r}")
    if expr.logic is FilterLogic.NOT:
        if len(expr.children) != 1:
            raise ValidationError("NOT 组必须恰好包含一个子表达式")
    elif not expr.children:
        raise ValidationError(f"{expr.logic.value} 组不得为空")
    children: list[FilterExpr] = []
    for child in expr.children:
        if not isinstance(child, (FilterClause, FilterGroup)):
            raise ValidationError(f"非法的子节点类型：{type(child).__name__}")
        children.append(_normalize_expr(child))
    return FilterGroup(expr.logic, children)


def validate(expr: FilterExpr) -> None:
    """递归校验：算子/逻辑合法、AND/OR 非空、NOT 单 child、value 类型、scope 禁入。"""
    _normalize_expr(expr)


def from_dict(node: Any) -> FilterExpr:
    """解析 F03 dict DSL 为 :data:`FilterExpr`（HTTP/SDK 边界用；不做 validate）。

    规则：``{field: value}`` → ``EQ``；``{field: {op: value}}`` → 指定算子；
    ``AND``/``OR`` 值为非空 list；``NOT`` 值为单个表达式；顶层多键默认 ``AND``。
    """
    if not isinstance(node, dict):
        raise ValidationError(f"dict DSL 节点必须是对象：{type(node).__name__}")
    if not node:
        raise ValidationError("dict DSL 节点不得为空")
    if len(node) > 1:  # 顶层多键（逻辑键 + 字段键 / 多字段）默认 AND 组合
        return FilterGroup(FilterLogic.AND, [from_dict({k: v}) for k, v in node.items()])

    key, val = next(iter(node.items()))
    if key in _LOGIC_KEYS:
        logic = _LOGIC_KEYS[key]
        if logic is FilterLogic.NOT:
            if not isinstance(val, dict):
                raise ValidationError("NOT 的值必须是单个表达式对象")
            return FilterGroup(FilterLogic.NOT, [from_dict(val)])
        if not isinstance(val, list) or not val:
            raise ValidationError(f"{key} 的值必须是非空列表")
        return FilterGroup(logic, [from_dict(x) for x in val])

    if isinstance(val, dict):  # {field: {op: value}}
        if len(val) != 1:
            raise ValidationError(f"字段 {key!r} 的算子对象必须恰好一个算子")
        op_str, operand = next(iter(val.items()))
        try:
            op = FilterOp(op_str)
        except ValueError:
            raise ValidationError(f"未知算子：{op_str!r}") from None
        return FilterClause(canonical_filter_field(key), op, operand)
    return FilterClause(canonical_filter_field(key), FilterOp.EQ, val)  # {field: value} → EQ


def normalize(raw: FilterExpr | list[FilterClause] | dict | None) -> FilterExpr | None:
    """把调用方输入规范化为 :data:`FilterExpr`，并做合法性校验。

    - ``None`` / 空 list / 空 dict → ``None``（无过滤）；
    - ``list[FilterClause]`` → ``FilterGroup(AND, clauses)``（旧扁平写法兼容）；
    - ``dict`` → 经 :func:`from_dict` 解析 DSL（HTTP/SDK 边界）；
    - 单个 :class:`FilterClause` / :class:`FilterGroup` → 校验并返回规范化副本。
    """
    if raw is None:
        return None
    if isinstance(raw, (FilterClause, FilterGroup)):
        expr: FilterExpr = raw
    elif isinstance(raw, dict):
        if not raw:
            return None
        expr = from_dict(raw)
    elif isinstance(raw, (list, tuple)):
        clauses = list(raw)
        if not clauses:
            return None
        for c in clauses:
            if not isinstance(c, FilterClause):
                raise ValidationError(f"filters 列表元素须为 FilterClause：{type(c).__name__}")
        expr = FilterGroup(FilterLogic.AND, clauses)
    else:
        raise ValidationError(f"不支持的 filters 类型：{type(raw).__name__}")
    return _normalize_expr(expr)


def and_merge(
    user_expr: FilterExpr | None, sys_clauses: list[FilterExpr]
) -> FilterExpr | None:
    """系统谓词（FilterExpr 列表，可含 OR 子树）与用户表达式做 ``AND`` 外包合并。

    用户表达式作为**整体** child 并入外层 AND，绝不摊平——避免用户的 ``OR``
    稀释 lifecycle/valid-time 等安全谓词（召回到 SUPERSEDED/FORGOTTEN 旧版本）。
    系统侧若需 OR 子树（如事件窗 ``OR(AND(GTE, LT), EQ 0)``），每个子树作为
    外层 AND 的一个 child 同样不摊平——保证安全谓词不被稀释。
    """
    parts: list[FilterExpr] = list(sys_clauses)
    if user_expr is not None:
        parts.append(user_expr)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return FilterGroup(FilterLogic.AND, parts)


def iter_clauses(expr: FilterExpr | None) -> Iterator[FilterClause]:
    """深度遍历产出全部叶子 :class:`FilterClause`（仅收集，不含逻辑语义）。"""
    if expr is None:
        return
    if isinstance(expr, FilterClause):
        yield expr
    elif isinstance(expr, FilterGroup):
        for child in expr.children:
            yield from iter_clauses(child)


def evaluate(expr: FilterExpr | None, leaf_pred: Callable[[FilterClause], bool]) -> bool:
    """对表达式递归求值：``AND``/``OR``/``NOT`` 短路，叶子委托 ``leaf_pred``。

    树遍历逻辑跨调用方共用（UnitReader 真源复核、内存后端过滤），叶子比较语义
    由各自的 ``leaf_pred`` 保留。``None`` → ``True``（无过滤即全通过）；未知
    ``logic`` 报错，绝不静默当作 ``NOT``。
    """
    if expr is None:
        return True
    if isinstance(expr, FilterClause):
        return leaf_pred(expr)
    if expr.logic is FilterLogic.AND:
        return all(evaluate(c, leaf_pred) for c in expr.children)
    if expr.logic is FilterLogic.OR:
        return any(evaluate(c, leaf_pred) for c in expr.children)
    if expr.logic is FilterLogic.NOT:
        return not evaluate(expr.children[0], leaf_pred)
    raise ValidationError(f"未知逻辑算子：{expr.logic!r}")


def extract_required_equality(expr: FilterExpr | None, field_name: str) -> Any | None:
    """表达式为真时 ``field_name`` 是否被**逻辑确定**为唯一等值；是则返回该值，否则 ``None``。

    供权限/pipeline 路由从 filters 安全取路由键：仅当表达式逻辑上强制唯一等值才返回，
    ``OR`` 多值、``NOT`` 下等值、``AND`` 冲突值一律 ``None``（改用显式 extension 或
    fallback），杜绝把 ``NOT(x=a)`` / ``OR(x=a, x=b)`` 误当确定路由。
    """
    if expr is None:
        return None
    if isinstance(expr, FilterClause):
        if expr.field == field_name and expr.op is FilterOp.EQ:
            return expr.value
        return None
    if expr.logic is FilterLogic.AND:  # 任一 child 确定即确定；多个 child 冲突 → None
        found: Any | None = None
        for child in expr.children:
            v = extract_required_equality(child, field_name)
            if v is None:
                continue
            if found is not None and found != v:
                return None
            found = v
        return found
    if expr.logic is FilterLogic.OR:  # 仅当所有分支都强制同一值才确定
        first = extract_required_equality(expr.children[0], field_name)
        if first is None:
            return None
        for child in expr.children[1:]:
            if extract_required_equality(child, field_name) != first:
                return None
        return first
    return None  # NOT：不构成 required equality
