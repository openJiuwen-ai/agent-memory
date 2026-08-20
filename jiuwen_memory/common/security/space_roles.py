"""空间内两轴角色、判定事实投影与取最具体成员记录（S09）。

一个空间内的权限拆成两条独立的轴：内容轴管条目的读写改删，治理轴管成员与
策略。拆开的原因是两者的授予对象不同——需要有人能管成员而看不到条目内容，
单轴表达不出这一档。

**落 `common/security/` 而非控制层或 API 层**，四条判据（S09「模块落点」）：

- 三张动作矩阵的元素类型取安全横切契约的 12 值 ``Action``，而
  ``control/types.py`` 已有一个五值 ``Action`` 且授权记录仍在用它，两者无法同模块共存；
- 矩阵的消费方是判定实现（安全层）与鉴权点的授予上界校验（API 层），角色枚举的
  消费方还包括成员记录的字段类型与存量兼容解析（控制层），三者能共同依赖的只有 ``common/``；
- 归属主体档清单与「取最具体记录」的划分规则同样被判定实现与鉴权点两侧读取；
- 判定事实投影是资源描述对象的字段类型，落控制层即上游公共类型反向依赖控制层。

本模块不访问存储，全部内容是代码常量与作用于本模块类型的纯函数。

.. note::
   三张动作矩阵的元素类型取本模块自定义的 :class:`SpaceAction`，与安全横切契约的 ``Action`` 是
   两套枚举，取值在本规约用到的九项上同名同义。取自定义枚举而非直接依赖安全横切契约：判据主体
   不绑定宿主形态（F03 决策 4），换宿主时整体替换枚举、矩阵内容不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..type_def import Scope


class SpaceContentRole(str, Enum):
    """内容轴：管空间内条目的读写改删。"""

    NONE = "none"
    VIEWER = "viewer"  # 只读
    CONTRIBUTOR = "contributor"  # 可贡献：可读可写，改删限本人所写
    EDITOR = "editor"  # 可编辑：可改删空间内任一条目


class SpaceGovernanceRole(str, Enum):
    """治理轴：管成员、策略与空间状态，不含条目读取权。

    中间档取名 ``manager`` 而非 ``admin``，与组织级 ``Role.ADMIN`` 区分——
    后者作用于整个组织，本枚举作用于单个空间。
    """

    NONE = "none"
    MANAGER = "manager"  # 管理员：管成员与策略
    OWNER = "owner"  # 拥有者：另可删空间


# 档位序号：供授予上界校验与自提禁止比较（同轴内比大小，两轴之间不可比）
CONTENT_RANK: dict[SpaceContentRole, int] = {
    SpaceContentRole.NONE: 0,
    SpaceContentRole.VIEWER: 1,
    SpaceContentRole.CONTRIBUTOR: 2,
    SpaceContentRole.EDITOR: 3,
}
GOVERNANCE_RANK: dict[SpaceGovernanceRole, int] = {
    SpaceGovernanceRole.NONE: 0,
    SpaceGovernanceRole.MANAGER: 1,
    SpaceGovernanceRole.OWNER: 2,
}


class SpaceAction(str, Enum):
    """判定动作枚举（S09「入口到轴与动作的映射」）。

    取值与安全横切契约的 ``Action`` 中本规约用到的九项同名同义。上游合入后本枚举整体由
    ``Action`` 替换，三张矩阵与入口映射表的内容不变。

    不复用 ``control/types.py`` 的五值 ``Action``：后者缺 ``REVOKE_SHARE`` 与三个
    组织级动作，且它是授权记录的字段类型，扩充会改变存量记录的取值域。
    """

    # 空间内动作：由两轴求值裁决
    READ = "read"
    WRITE = "write"
    UPDATE = "update"
    DELETE = "delete"
    SHARE = "share"
    REVOKE_SHARE = "revoke_share"
    # 组织级动作：由管理面角色闸门终局裁决，不落两轴求值
    MANAGE_SPACE = "manage_space"
    READ_AUDIT = "read_audit"
    ADMINISTER_SYSTEM = "administer_system"


# 三张动作矩阵。写成代码常量而非配置或存储记录：它是判定的基础规则，不随部署变化，
# 也不应在鉴权路径上产生额外的存储访问。
CONTENT_ACTIONS: dict[SpaceContentRole, frozenset[SpaceAction]] = {
    SpaceContentRole.NONE: frozenset(),
    SpaceContentRole.VIEWER: frozenset({SpaceAction.READ}),
    SpaceContentRole.CONTRIBUTOR: frozenset({SpaceAction.READ, SpaceAction.WRITE}),
    SpaceContentRole.EDITOR: frozenset(
        {SpaceAction.READ, SpaceAction.WRITE, SpaceAction.UPDATE, SpaceAction.DELETE}
    ),
}

# 限「本人所写」的附加动作：仅 contributor 有，且只在条目作者主体等于调用方时并入。
# editor 不在内——它对空间内任一条目都可改删，附加集合是空集而非重复项。
CONTENT_ACTIONS_OWN: dict[SpaceContentRole, frozenset[SpaceAction]] = {
    SpaceContentRole.NONE: frozenset(),
    SpaceContentRole.VIEWER: frozenset(),
    SpaceContentRole.CONTRIBUTOR: frozenset({SpaceAction.UPDATE, SpaceAction.DELETE}),
    SpaceContentRole.EDITOR: frozenset(),
}

# 治理轴：DELETE 指删空间，与内容轴的 DELETE（删条目）取值相同、管辖对象不同。
# 两者不混淆是因为一次判定只求一条轴，动作集合按轴取。
GOVERNANCE_ACTIONS: dict[SpaceGovernanceRole, frozenset[SpaceAction]] = {
    SpaceGovernanceRole.NONE: frozenset(),
    SpaceGovernanceRole.MANAGER: frozenset(
        {SpaceAction.READ, SpaceAction.UPDATE, SpaceAction.SHARE, SpaceAction.REVOKE_SHARE}
    ),
    SpaceGovernanceRole.OWNER: frozenset(
        {
            SpaceAction.READ,
            SpaceAction.UPDATE,
            SpaceAction.SHARE,
            SpaceAction.REVOKE_SHARE,
            SpaceAction.DELETE,
        }
    ),
}

# 归属主体档的两级入口清单。消费方是判定实现的归属主体档求值，API 层只用它做
# 一致性断言，因此定义落本模块而不落 API 层。
# 两级不重叠：主体两维逐维相同者自然满足覆盖，因此第二级只列它独有的入口。
OWNER_ENTRY_SAME_DIMS = frozenset(
    {  # 仅本人直接调用（主体两维逐维相同）
        "update_space",
        "archive_space",
        "set_space_policy",
        "delete_space",
        "list_space_members",
        "add_space_member",
        "remove_space_member",
        "export_space",
    }
)
OWNER_ENTRY_COVERS = frozenset(
    {  # 覆盖即可（如用户经其名下代理调用）
        "get_space",
        "list_spaces",
        "space_usage",
        "get_space_policy",
    }
)


class SpaceAxis(str, Enum):
    """入口判哪条轴。

    ``EITHER`` 不是第三条轴：判定一次只求一条轴，由鉴权点按「治理轴在前」依次尝试，
    任一通过即放行并记下通过的轴。记下通过的轴是为了 ``get_space`` / ``list_spaces``
    的空间策略裁剪——只经内容轴通过时策略置空。

    ``ORG`` 的入口不落两轴求值，由管理面角色闸门终局裁决。组织级角色不属本特性范围，
    这些入口维持现状判据（F03 决策 4「五步无判据来源」）。
    """

    CONTENT = "content"
    GOVERNANCE = "governance"
    EITHER = "either"  # 两轴任一：返回空间元数据、不含条目内容的入口
    ORG = "org"  # 组织级：角色闸门终局，不落两轴求值


@dataclass(frozen=True)
class EntryRule:
    """一个入口判哪条轴、要哪个动作、「本人所写」附加集合是否适用。

    ``own_actions_apply`` 取假的入口，其目标无法归属到单一作者——批量作用于整个空间，
    条目的作者各不相同。对它并入「本人所写」附加集合等于按最宽的一条放行整批，因此
    这类入口只按基础集合求值。
    """

    axis: SpaceAxis
    action: SpaceAction
    own_actions_apply: bool = True


# 入口到轴与动作的映射（S09「入口到轴与动作的映射」）。
#
# 集中为一张表而不散在各方法里：漏配一个入口的后果是它绕开某条轴的判定，
# 而散落的形态无法一眼核对完整性。表外的入口由鉴权点按「未登记即不做空间级判定」
# 处置，与组织级入口一并维持现状判据。
ENTRY_RULES: dict[str, EntryRule] = {
    # -- 组织级：角色闸门终局，本特性内维持现状判据 ------------------------- #
    "create_space": EntryRule(SpaceAxis.ORG, SpaceAction.MANAGE_SPACE),
    "audit": EntryRule(SpaceAxis.ORG, SpaceAction.READ_AUDIT),
    "admin_get": EntryRule(SpaceAxis.ORG, SpaceAction.ADMINISTER_SYSTEM),
    "admin_all": EntryRule(SpaceAxis.ORG, SpaceAction.ADMINISTER_SYSTEM),
    "admin_set": EntryRule(SpaceAxis.ORG, SpaceAction.ADMINISTER_SYSTEM),
    # -- 内容轴 ------------------------------------------------------------- #
    # 异步与批量写入不单列：三者的鉴权都以入口名 add 调用，同轴同动作。
    "add": EntryRule(SpaceAxis.CONTENT, SpaceAction.WRITE),
    "search": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    "list": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    "get": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    "inspect": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    "trace": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    # 取内容轴读动作、又不带条目信息，因此第 7 步对它另设排除：落第 10 步的
    # 归属主体档第一级（主体维逐维相同）裁决。
    "export_space": EntryRule(SpaceAxis.CONTENT, SpaceAction.READ),
    "update": EntryRule(SpaceAxis.CONTENT, SpaceAction.UPDATE),
    "delete": EntryRule(SpaceAxis.CONTENT, SpaceAction.DELETE),
    # 去重与遗忘两种演进模式取 UPDATE，由鉴权点按模式覆盖本表的默认动作；
    # 两种模式都不放宽到「本人所写」——输入是一批条目，逐条判会使一次调用部分生效。
    "evolve": EntryRule(SpaceAxis.CONTENT, SpaceAction.WRITE, own_actions_apply=False),
    # 任务状态查询与取消按发起该作业的演进模式取动作，因此与 evolve 同默认值、
    # 同样由鉴权点覆盖。这要求任务信息携带演进模式取值而非任务类名。
    "job_status": EntryRule(SpaceAxis.CONTENT, SpaceAction.WRITE, own_actions_apply=False),
    "job_cancel": EntryRule(SpaceAxis.CONTENT, SpaceAction.WRITE, own_actions_apply=False),
    # -- 两轴任一：返回空间元数据、不含条目内容 ----------------------------- #
    "get_space": EntryRule(SpaceAxis.EITHER, SpaceAction.READ),
    "list_spaces": EntryRule(SpaceAxis.EITHER, SpaceAction.READ),
    "space_usage": EntryRule(SpaceAxis.EITHER, SpaceAction.READ),
    # -- 治理轴 ------------------------------------------------------------- #
    "get_space_policy": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.READ),
    "list_space_members": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.READ),
    # 空间状态变更不单列：本接口经 update_space 的 status 字段与 archive_space 表达。
    "update_space": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.UPDATE),
    "archive_space": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.UPDATE),
    "set_space_policy": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.UPDATE),
    "add_space_member": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.SHARE),
    "grant": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.SHARE),
    "remove_space_member": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.REVOKE_SHARE),
    "revoke": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.REVOKE_SHARE),
    "delete_space": EntryRule(SpaceAxis.GOVERNANCE, SpaceAction.DELETE),
}


@dataclass(frozen=True)
class SpaceMemberFact:
    """一条成员记录在判定视角下的最小投影：只有 scope 与两轴角色。"""

    scope: Scope
    content_role: SpaceContentRole
    governance_role: SpaceGovernanceRole


@dataclass(frozen=True)
class SpaceAuthorizationFacts:
    """判定所需的空间事实，最小投影。

    由鉴权点（PEP）从控制层的 ``SpaceFacts`` 折算后传入判定实现（PDP）。
    空间策略、生命周期状态与成员记录的时间戳都不在内——判定不看它们，
    生命周期状态由鉴权点在授权通过之后另行校验。
    """

    owners: tuple[Scope, ...] = ()
    members: tuple[SpaceMemberFact, ...] = ()

    @property
    def is_individual(self) -> bool:
        """成员表为空即个体空间——归属对比的前提之一。"""
        return not self.members


def most_specific(
    members: tuple[SpaceMemberFact, ...], actor: Scope, *, dim: str
) -> SpaceMemberFact | None:
    """按维划分候选记录后取最具体的一条，无候选返回 ``None``。

    判定链的两轴求值与鉴权点的治理上界校验共用本实现，不各写一份划分规则。

    划分规则：

    - ``dim="user"``：user 维与 actor 相同且 agent 维为空的记录，加组织通配记录（两维皆空）
    - ``dim="agent"``：agent 维与 actor 相同的记录，含 user 维同时与 actor 相同的双维记录；
      组织通配记录不进 agent 维候选，user 维与 actor 不同的双维记录也不进

    「通配只在 user 维参与命中」由 agent 分支的第一个条件实现，不是另一条独立规则：
    ``m.scope.agent`` 为真即排除了两维皆空的通配记录。少这一条，具名代理的档位提升
    会被通配记录压回，组织内全体共享与代理跨用户复用两类场景都依赖它。

    最具体的定义是非空主体维多者优先：双维记录优先于单维 agent 记录，单维记录优先于
    组织通配记录。同一具体度下至多一条——成员表按 scope 单键。

    双维记录只服务存量数据：写入侧已拒绝新增该形态，反查索引也不设双维桶；但存量库里
    已有的双维记录仍须按「更窄、取最具体时优先」求值，否则回填期内同一主体的档位会随
    记录形态跳变。
    """
    if dim == "user":
        candidates = [
            m
            for m in members
            if not m.scope.agent and (not m.scope.user or m.scope.user == actor.user)
        ]
    else:
        candidates = [
            m
            for m in members
            if m.scope.agent
            and m.scope.agent == actor.agent
            and (not m.scope.user or m.scope.user == actor.user)
        ]
    return max(
        candidates,
        key=lambda m: bool(m.scope.user) + bool(m.scope.agent),
        default=None,
    )
