"""检索侧两族系统谓词的生成（F07「检索两族谓词」）。

调用方表达式之外，内核为每次检索追加两族谓词：个体空间的作者收窄（第一族）与归属坐标
折算出的收窄维（第二族）。两族合起来与调用方表达式合成一个 AND 一次下推，在 top-k 截断
之前生效——召回后二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 ``top_k``。

**与 :mod:`.space_decision` 的分工**：那里是空间级授权的判据（能不能进这个空间），本模块
是进入空间之后的可见范围收窄（这个空间里哪些条目可见）。两者都不访问存储、都由鉴权点
调用，但结论类型与失效方向不同：判据失效是越权，收窄失效是放宽。

**落 `common/security/` 而非 API 层或控制层**：:func:`system_predicates` 收 ``actor``
（调用方身份），S02 不变量 2 与 F07 不变量 5 分别排除控制层与构建层；其输入类型
:class:`~common.security.space_roles.SpaceAuthorizationFacts` 已在本层，落此处无新增
依赖边。第二族的谓词构造（:func:`_narrow_predicates`）与安全语义无关，作为
:func:`system_predicates` 的实现细节私有，不单独对外——拆开即调用方须自行拼装两族，而
两族必须一起下推。
"""

from __future__ import annotations

from typing import Mapping

from ..type_def import FilterClause, FilterOp, Scope
from . import principal
from .space_roles import SpaceAuthorizationFacts


def individual_space_predicates(
    facts: SpaceAuthorizationFacts | None, actor: Scope
) -> list[FilterClause]:
    """第一族：个体空间的系统谓词，条件由内核自算，接口上没有让调用方干预的口子。

    仅个体空间生效（成员表为空）。协作空间的可见范围由两轴角色裁决，按作者收窄会使其失去
    协作意义。

    | 调用形态 | 追加的谓词 |
    |---|---|
    | 代理自主运行 | ``author_principal == "agent:<id>"`` |
    | 用户本人直接调用，或经其名下代理调用 | 不追加，该空间内全部条目可见 |

    多归属空间（回填产物）另有一条：恒追加「作者主体等于调用方」，不看调用方形态——缺它
    则回填窗口内两个归属者互相召回得到对方的条目，且不报错。
    """
    if facts is None or not facts.is_individual:
        return []
    try:
        author_principal, _ = principal.derive_author(actor)
    except Exception:  # noqa: BLE001 —— 主体两维皆空的调用在形态校验处已拒绝
        # 谓词生成不是判定，抛错会把一个已经通过鉴权的调用变成失败。
        return []
    if len(facts.owners) > 1 or author_principal.startswith("agent:"):
        return [_author_clause(author_principal)]
    return []


def system_predicates(
    facts: SpaceAuthorizationFacts | None,
    actor: Scope,
    narrow: Mapping[str, str] | None = None,
) -> list[FilterClause]:
    """两族谓词合起来。调用方表达式与它们合成一个 AND 一次下推。

    ``narrow`` 是已折算的收窄维取值（``标签键 -> 取值``），由
    :func:`~construction.router.narrow_dims_of` 从已以身份覆盖过内核三项的坐标算出。
    """
    return individual_space_predicates(facts, actor) + _narrow_predicates(narrow or {})


def _author_clause(author_principal: str) -> FilterClause:
    return FilterClause(
        f"system_metadata.{principal.AUTHOR_PRINCIPAL}", FilterOp.EQ, author_principal
    )


def _narrow_predicates(narrow: Mapping[str, str]) -> list[FilterClause]:
    """第二族：收窄维谓词，每个有取值的维生成 ``system_metadata.<tag_key> IN ["", <value>]``。

    取值为空串的条目一并命中，因此「该维不适用」与「判为否」的条目不会被收窄掉。坐标缺项
    不生成对应谓词，表现为该维不收窄——失效方向是放宽，不是越权。
    """
    clauses: list[FilterClause] = []
    for key in sorted(narrow):
        value = str(narrow[key] or "").strip()
        if value:
            clauses.append(FilterClause(f"system_metadata.{key}", FilterOp.IN, ["", value]))
    return clauses
