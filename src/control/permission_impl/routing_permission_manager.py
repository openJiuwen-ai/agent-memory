"""按 PermissionContext 路由到不同权限策略的 PermissionManager。"""

from __future__ import annotations

from common.errors import ValidationError
from common.type_def import Scope
from control.base import ControlOperatorType
from control.permission import PermissionManager, PermissionProducer
from control.types import Action, Grant, PermissionContext

from .allow_all_permission_manager import AllowAllPermissionManager


def _query_route_is_unresolved(
    context: PermissionContext | None,
    route_key: str,
    value: str,
) -> bool:
    """查询需要类型化路由但未能解析唯一值时返回 True。"""
    if context is None:
        return False
    if context.resource_type != "query":
        return False
    if route_key == "resource_type":
        return False
    return not value


class RoutingPermissionManager(PermissionManager):
    """统一权限入口下的策略路由。

    该实现不自行定义授权语义，只根据 ``PermissionContext`` 选择一个已配置的
    PermissionManager，并把 check/grant/revoke 委托给它。grant/revoke 广播给
    全部 delegate，避免用户需要理解授权记录应落在哪个后端。
    """

    def __init__(
        self,
        policies: dict[str, PermissionManager],
        routes: dict[str, str],
        fallback: str,
        route_key: str = "memory_type",
    ) -> None:
        if fallback not in policies:
            raise ValidationError(
                f"RoutingPermissionManager fallback {fallback!r} 不存在"
                f"（已定义：{sorted(policies)}）"
            )
        if isinstance(policies[fallback], AllowAllPermissionManager):
            # fallback 承接的是**路由值缺失**的请求，而调用方只要不写 filters 就能触发。
            # 此时没有路由值可回注为系统谓词、查询范围不受约束，若 fallback 又是
            # allow_all，等于把「不声明类型」变成免鉴权后门（S03「PermissionManager」
            # 亦称 allow_all 仅为 dev-only）。fallback 必须是最小权限策略。
            raise ValidationError(
                f"RoutingPermissionManager fallback {fallback!r} 不得是 allow_all："
                "路由值缺失的请求全部落在 fallback，它必须是最小权限策略"
            )
        self._policies = policies
        self._routes = dict(routes)
        self._fallback = fallback
        self._route_key = route_key

    def routing_fields(self) -> tuple[str, ...]:
        # 供 API 层把路由值回注为系统谓词，绑定「按哪条策略授权」与「能读到哪些数据」。
        return (self._route_key,)

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PERMISSION

    def health(self) -> None:
        for policy in self._policies.values():
            policy.health()

    def grant(self, grant: Grant) -> None:
        for policy in _unique_policies(self._policies):
            policy.grant(grant)

    def revoke(self, grant: Grant) -> None:
        for policy in _unique_policies(self._policies):
            policy.revoke(grant)

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        # S03「PermissionManager」约束「routing 不改变授权语义，只选择 delegate」——
        # 此处只做选择并委托：
        # 不额外 deny、不对多个 policy 求交集，root / owner-cover / Grant 等基础规则
        # 全部由被选中的 delegate 按 S03 的 check 规则判定。路由值未解析时按 _select
        # 落到 fallback（S03 示例即如此定义），不在路由层加码。
        policy = self._select(context)
        return policy.check(actor, target, action, context=context)

    def _select(self, context: PermissionContext | None) -> PermissionManager:
        value = _context_value(context, self._route_key)
        # 只接受 routes 里**显式声明**的路由值，未命中一律落 fallback。不同于 Pipeline
        # 路由（S03:136 允许「路由值本身是 profile 名则直接使用」）——授权侧若沿用该
        # 兜底，调用方就能直接点名 policy 来挑选审查自己的策略，等于让被审查者选审查员。
        policy_name = self._routes.get(value, self._fallback) if value else self._fallback
        if policy_name not in self._policies:
            policy_name = self._fallback
        return self._policies[policy_name]


def _unique_policies(policies: dict[str, PermissionManager]) -> list[PermissionManager]:
    seen: set[int] = set()
    result: list[PermissionManager] = []
    for policy in policies.values():
        marker = id(policy)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(policy)
    return result


def _context_value(context: PermissionContext | None, route_key: str) -> str:
    if context is None:
        return ""
    if route_key == "memory_type":
        return context.memory_type
    if route_key == "pipeline":
        return context.pipeline
    if route_key == "resource_type":
        return context.resource_type
    return str(context.metadata.get(route_key, "")).strip()


@PermissionProducer.register("routing")
def _build(config):
    route_key = config.get("route_key", "memory_type")
    fallback = str(config.get("fallback", "")).strip()
    if not fallback:
        raise ValidationError(
            "permission.routing params.fallback 必须指向一个具名 permission"
        )
    if fallback == config.name:
        raise ValidationError("permission.routing params.fallback 不能指向 routing 自身")
    routes_raw = config.get("routes", {})
    if not isinstance(routes_raw, dict):
        raise ValidationError("permission.routing params.routes 必须是映射")
    routes = {str(key): str(value) for key, value in routes_raw.items()}
    if any(policy_name == config.name for policy_name in routes.values()):
        raise ValidationError("permission.routing params.routes 不能指向 routing 自身")
    policy_names = set(routes.values()) | {fallback}
    policies = {
        policy_name: PermissionProducer.build_named(policy_name, config.ctx)
        for policy_name in policy_names
    }
    return RoutingPermissionManager(
        policies=policies,
        routes=routes,
        fallback=fallback,
        route_key=route_key,
    )
