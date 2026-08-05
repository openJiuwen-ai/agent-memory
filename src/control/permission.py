"""PermissionManager — 权限管理（架构 §3.2）。

scope 模型的执行点：检索/写入默认限制在自身 scope 内，跨 scope 访问
（多 Agent 共享池等）必须显式授权。授权/回收/校验都产生审计事件。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope
from common.security.types import AuthContext

from .base import ControlOperator
from .types import Action, Grant, PermissionContext


class PermissionProducer(Factory):
    """PermissionManager 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``permission_impl`` 下以
    ``@PermissionProducer.register("<名>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "permission"


class PermissionManager(ControlOperator):
    @abstractmethod
    def grant(self, grant: Grant) -> None:
        """新增一条跨 scope 授权。"""

    @abstractmethod
    def revoke(self, grant: Grant) -> None:
        """回收一条授权（幂等）。匹配哪条既有授权（按 grantor+grantee、是否
        逐 action 撤销等）由具体实现定义。"""

    @abstractmethod
    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
        *,
        auth: AuthContext | None = None,
    ) -> bool:
        """校验 ``actor`` 是否可对 ``target`` scope 执行 ``action``。

        ``auth`` 是认证层产出的可信上下文（由 PEP 从 ContextVar 取出后透传），
        携带 ``actor`` 之外的两样判定依据：``role``（§3.1 三级角色）与
        ``acting_user``（§4.3 用户授权 Agent 代操作）。二者都**不可**从 ``actor``
        这个 Scope 推断出来，故必须单独传入。

        ``auth`` 为 ``None`` 时行为退回纯 ACL——即认证接入前的语义。这条兼容线
        承载后台 job、单测与 ``build_kernel`` 直连等非请求场景：它们没有认证上下文，
        不该因此被拒。实现**不得**自行去读 ContextVar：PDP 应当是其入参的纯函数，
        否则单测要先布置环境态才能跑，判定依据也不再显式可见。
        """

    def routing_fields(self) -> tuple[str, ...]:
        """本实现据以**选择策略**的 :class:`PermissionContext` 字段名（默认不路由）。

        路由型实现按请求里的某个字段挑选 delegate，而该字段由调用方提供——若不同时
        约束查询能触达的数据，调用方就能"用 A 的钥匙开 B 的门"：路由值填宽松策略对应
        的类型、``filters`` 却指向受严格策略保护的数据。API 层据此把路由值**回注为
        系统谓词**，使授权依据与数据范围绑定。
        """
        return ()
