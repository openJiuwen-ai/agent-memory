"""Grant 与 Delegation 的真源契约（F05 §Grant / §Delegation）。

两个 Store 刻意**分开**，不合成一个 "PermissionStore"：

- :class:`GrantStore` 是**资源侧**的长期开放（「A 把自己数据的读权限给 B」），
  可以没有过期时间；
- :class:`DelegationStore` 是**身份侧**的有限期代理（「user 授权 agent 代表自己」），
  必须有过期时间、必须能撤销、动作走 allowlist。

合成一个类型会让「撤销了代理但分享还在」这类正确行为难以表达，也会让「永久有效」
这个 Grant 的合法状态泄漏成 Delegation 的合法状态。

两者都是 Authorizer 的**只读依赖**（写入走管理面 API）。查询接口按判定需要设计：
Authorizer 问的是「针对这次判定，有没有一条覆盖它的记录」，不是「把 grantee 的所有
授权列出来让我筛」——后者把过滤逻辑推给调用方，每个调用方都有机会漏一个条件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from common.factory.factory import Factory
from common.security.types import Action, Delegation, Grant
from common.type_def.scope import Scope


class GrantStoreProducer(Factory):
    """GrantStore 的注册式工厂。"""

    TOP_NAME = "grant_store"


class DelegationStoreProducer(Factory):
    """DelegationStore 的注册式工厂。"""

    TOP_NAME = "delegation_store"


class GrantStore(ABC):
    """显式长期授权的真源。"""

    @abstractmethod
    def add(self, grant: Grant) -> None:
        """写入一条授权（按 ``grant_id`` 幂等）。"""

    @abstractmethod
    def revoke(self, grant_id: str) -> None:
        """撤销一条授权（幂等；不存在时静默返回）。

        软撤销：记录保留、置撤销标记。硬删除会让「这条权限什么时候没的」在审计里
        断线，而权限的消失时刻恰恰是事故复盘要问的第一个问题。
        """

    @abstractmethod
    def find_active(
        self,
        *,
        grantee: Scope,
        grantor_org: str,
        action: Action,
        now: datetime,
    ) -> list[Grant]:
        """取可能覆盖本次判定的**有效**授权（未撤销、未过期、动作匹配）。

        按 ``grantor_org`` 而非完整 grantor Scope 过滤：判定时只知道资源归属的 org
        是硬边界，具体哪条 grantor Scope 覆盖 target 要由 scope 覆盖规则算，那是
        Authorizer 的策略而不是存储的查询条件。

        实现**必须**在存储层就滤掉已撤销与已过期的记录，不能返回全量让调用方筛——
        「取回来再过滤」的写法里，漏一个条件就是一个静默的越权。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全能力同构。"""


class DelegationStore(ABC):
    """代操作授权的真源。

    存在的理由是 F05 §从 header 直接产生 Delegation 那条拒绝：网关 header 最多证明
    「网关声称这是某个 user」，证明不了「该 user 真的授权了这个 agent」。委托必须是
    服务端事实，由 ``delegation_id`` 回这里复核。
    """

    @abstractmethod
    def add(self, delegation: Delegation) -> None:
        """写入一条委托（按 ``delegation_id`` 幂等）。"""

    @abstractmethod
    def revoke(self, delegation_id: str) -> None:
        """撤销一条委托（幂等）。同 :meth:`GrantStore.revoke`，软撤销。"""

    @abstractmethod
    def get(self, delegation_id: str) -> Delegation | None:
        """按标识取委托；不存在返回 ``None``。

        **返回原始记录而不是「是否有效」的布尔**：时效判定要用 Authorizer 那一个
        统一的 ``now``（见 :class:`~common.security.types.AuthorizationEnvironment`），
        存储自己取一次 ``now`` 会和 Grant 的时效判定错开。

        不存在与已撤销都由上层归到同一个 reason code——区分二者是委托枚举侧信道。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全能力同构。"""
