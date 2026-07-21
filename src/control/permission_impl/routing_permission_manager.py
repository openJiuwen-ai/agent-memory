"""按 PermissionContext 路由到不同权限策略的 PermissionManager。"""

from __future__ import annotations

from common.errors import ValidationError
from common.type_def import Scope
from control.base import ControlOperatorType
from control.permission import PermissionManager, PermissionProducer
from control.types import Action, Grant, PermissionContext


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
        self._policies = policies
        self._routes = dict(routes)
        self._fallback = fallback
        self._route_key = route_key

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
        policy = self._select(context)
        return policy.check(actor, target, action, context=context)

    def _select(self, context: PermissionContext | None) -> PermissionManager:
        value = _context_value(context, self._route_key)
        policy_name = self._routes.get(value, value) if value else self._fallback
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
