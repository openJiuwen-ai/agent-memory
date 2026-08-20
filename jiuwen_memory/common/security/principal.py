"""调用方身份的推导、形态校验与三种粒度的比较（S09）。

调用方身份是一个五维 ``Scope``，与它相关的计算散落各处必然随时间发散。本模块把
三件事收进一处：从身份推导条目的作者标记、拒绝主体维全空的数据面调用、三种粒度
的身份比较。做成无状态纯函数是因为这些计算不访问存储，可独立测试。

**落 `common/security/` 而非控制层**，判据与 :mod:`.space_roles` 同一条：消费方跨三层
——判定实现的归属对比（安全层）、鉴权点与多空间读写的形态校验（API 层）、空间创建时的
归属登记（控制层）。三者能共同依赖的只有 ``common/``；落控制层即安全层反向依赖控制层。
"""

from __future__ import annotations

from ..errors import PermissionDeniedError, ValidationError
from ..type_def import Scope

# -- 条目上与身份相关的两个 metadata 键 ------------------------------------- #
AUTHOR_PRINCIPAL = "author_principal"  # 作者主体，内核推导，判定读它
AUTHOR_AGENT = "author_agent"  # 作者代理，内核推导，判定不读（记录项）

# 条目级判定读的键，由鉴权点搬进资源描述对象的属性。
# AUTHOR_AGENT 不在内——它只是记录项，判定不读。
AUTHOR_KEYS = (AUTHOR_PRINCIPAL,)

# 身份比较的主体维。**不含 space 维**，理由见 covers_owner 的说明。
_PRINCIPAL_DIMS = ("org", "user", "agent")


def derive_author(identity: Scope) -> tuple[str, str]:
    """按调用方身份推导两个作者标记：``(主体项, 代理项)``。

    代理链上有人类主体即归人类：用户经其代理写入的条目，作者主体是该用户，
    代理项记录经哪个代理写入。代理自主运行时主体项取 ``agent:`` 前缀、代理项为空串。
    """
    if identity.user:
        return f"user:{identity.user}", identity.agent
    if identity.agent:
        return f"agent:{identity.agent}", ""
    raise ValidationError("identity must carry user or agent")


def require_principal(identity: Scope) -> None:
    """条目读写路径的形态校验：主体维全空即拒绝。"""
    if not identity.user and not identity.agent:
        raise PermissionDeniedError("identity requires user or agent for data-plane entries")


def owner_entry_of(identity: Scope, org: str, space: str) -> Scope | None:
    """归属登记项：取单维；主体维全空返回 ``None``（运维通道建的空间不登记）。

    取单维而不是把调用方身份整体记下，是为了让本人直接调用与经代理调用对上同一条登记。
    """
    if identity.user:
        return Scope(org=org, space=space, user=identity.user)
    if identity.agent:
        return Scope(org=org, space=space, agent=identity.agent)
    return None


def covers_owner(entry: Scope, actor: Scope) -> bool:
    """粗筛：登记项的每个非空主体维 actor 都有相同取值；actor 可另带其他维。

    用户经其名下代理调用由此通过。归属对比的前提之一、归属主体档第二级。

    **不比较 space 维，否则归属对比恒不成立。** 两侧的 space 维来源不同：登记项由
    :func:`owner_entry_of` 产出、space 维恒非空，而调用方身份不带 space 维、恒为空串。
    逐维比较时该维一空一有值即判为不同，归属对比与归属主体档两级一并失效——用户读写
    自己的主空间被拒，且症状与「回填未完成」不可区分。

    不比较不削弱约束：调用点的 ``owners`` 均取自按目标 ``(org, space)`` 读出的空间事实，
    登记项的 space 维恒等于目标空间，比较它是同一取值与自身相比。目标空间与调用方的
    对应关系由 org 边界与「事实按目标空间读取」两条共同保证。
    """
    for dim in _PRINCIPAL_DIMS:
        value = getattr(entry, dim)
        if value and value != getattr(actor, dim):
            return False
    return True


def same_dims(entry: Scope, actor: Scope) -> bool:
    """细判：三个主体维逐维相同。归属主体档第一级（治理动作与整空间导出）。

    与 :func:`covers_owner` 同样不比较 space 维，理由见其说明。
    """
    return all(getattr(entry, dim) == getattr(actor, dim) for dim in _PRINCIPAL_DIMS)


def author_match(actor: Scope, author_principal: str) -> bool:
    """归属对比的作者标记比对：只比作者主体项。

    不比作者代理：用户经其名下任一代理发起的调用，与本人直接调用推导出同一个作者主体
    （见 :func:`derive_author`），因此同一用户名下换代理不影响可达性。条目的可读范围
    由所在空间的权限决定，条目上不另设可见性声明。
    """
    expected = f"user:{actor.user}" if actor.user else (
        f"agent:{actor.agent}" if actor.agent else ""
    )
    return expected == author_principal  # 主体项：留空不视为涵盖
