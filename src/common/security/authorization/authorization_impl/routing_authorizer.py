"""按资源属性路由到不同 Authorizer 的组合实现（迁移计划 §5.2「权限路由」）。

取代 ``control.permission_impl.routing_permission_manager``。语义一字未改，只是判定
主体从 ``PermissionManager`` 换成 :class:`~common.security.authorization.Authorizer`、
路由依据从 ``PermissionContext`` 换成 :class:`ResourceDescriptor`：

- ``resource_type`` 直接读 descriptor 的同名字段；
- 其余 route_key 读 ``attributes``——PEP 从真源构造 descriptor，所以 ``memory_type``
  这类值对已有资源是存储里的事实，不是请求里的声明。

本实现**不定义任何授权语义**：不额外 deny、不对多个 delegate 求交集。owner-cover、
角色闸门、Grant、默认拒绝全部由被选中的 delegate 判定。它只回答「这次判定归谁管」。
"""

from __future__ import annotations

from common.errors import ValidationError
from common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from common.security.types import AuthContext, AuthorizationEnvironment, ResourceDescriptor


class RoutingAuthorizer(Authorizer):
    """统一授权入口下的策略路由。"""

    def __init__(
        self,
        policies: dict[str, Authorizer],
        routes: dict[str, str],
        fallback: str,
        route_key: str = "memory_type",
    ) -> None:
        if fallback not in policies:
            raise ValidationError(
                f"RoutingAuthorizer fallback {fallback!r} 不存在（已定义：{sorted(policies)}）"
            )
        if policies[fallback].is_test_only():
            # fallback 承接的是**路由值缺失**的请求，而调用方只要不声明类型就能触发。
            # 此时没有路由值可回注为系统谓词、查询范围不受约束，若 fallback 又恒放行，
            # 等于把「不声明类型」变成免鉴权后门。这里问的是 capability 而非 target 名
            # （S08 不变量 7）：第三方注册的恒放行实现同样要被拦住。
            raise ValidationError(
                f"RoutingAuthorizer fallback {fallback!r} 不得是仅测试实现："
                "路由值缺失的请求全部落在 fallback，它必须是最小权限策略"
            )

        # Round4 P1-3: 装配期检查所有 policy 是否共享同一 GrantStore（真源统一）
        # 多 Store 顺序写入无原子性：第二个 Store 失败时第一个已提交，造成部分授权。
        # 要么全部 policy 共享同一 Store（通过具名引用），要么拒绝启动。
        all_stores = []
        for policy_name, policy in policies.items():
            stores = policy.management_grant_stores()
            if stores:
                all_stores.extend((policy_name, id(store)) for store in stores)

        if all_stores:
            # 检查所有 policy 的 Store 是否为同一实例（按 id 判断）
            unique_store_ids = {store_id for _, store_id in all_stores}
            if len(unique_store_ids) > 1:
                store_owners = {}
                for policy_name, store_id in all_stores:
                    store_owners.setdefault(store_id, []).append(policy_name)
                detail = "; ".join(
                    f"Store#{i + 1} 被 {', '.join(sorted(owners))} 使用"
                    for i, owners in enumerate(store_owners.values())
                )
                raise ValidationError(
                    f"RoutingAuthorizer 的所有 policy 必须共享同一个 GrantStore（真源统一）。"
                    f"当前配置了 {len(unique_store_ids)} 个不同 Store，grant/revoke 会部分提交。"
                    f"请在配置中让所有 policy 引用同一具名 grant_store。详情：{detail}"
                )

        self._policies = policies
        self._routes = dict(routes)
        self._fallback = fallback
        self._route_key = route_key

    def routing_fields(self) -> tuple[str, ...]:
        # 供 PEP 把路由值回注为系统谓词，绑定「按哪条策略授权」与「能读到哪些数据」。
        return (self._route_key,)

    def health(self) -> None:
        for policy in self._policies.values():
            policy.health()

    def management_grant_store(self):
        # 管理写透传到 fallback delegate 的 Store：fallback 承接路由值缺失的请求，是
        # 最小权限策略，公共 grant/revoke 写它的真源与其他请求的判定一致。
        # **已弃用**：路由场景应调用 management_grant_stores()（P1-4 真源统一）。
        return self._policies[self._fallback].management_grant_store()

    def management_grant_stores(self):
        # P1-4：路由场景下公共 grant 须写入**全部** policy 的 Store，否则「grant 时按
        # fallback 判定、实际访问时路由到别的 policy」就读不到授权——两次判定看不同真源。
        # 去重：多个 policy 可能共享同一 Store（装配期通过具名引用同一实例）。
        stores = []
        seen_ids = set()
        for policy in self._policies.values():
            for store in policy.management_grant_stores():
                store_id = id(store)
                if store_id not in seen_ids:
                    stores.append(store)
                    seen_ids.add(store_id)
        return stores

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        policy = self._select(resource)
        return policy.authorize(auth=auth, resource=resource, environment=environment)

    def _select(self, resource: ResourceDescriptor) -> Authorizer:
        value = _route_value(resource, self._route_key)
        # 只接受 routes 里**显式声明**的路由值，未命中一律落 fallback。不同于 Pipeline
        # 路由（S03:136 允许「路由值本身是 profile 名则直接使用」）——授权侧若沿用该
        # 兜底，调用方就能直接点名 policy 来挑选审查自己的策略，等于让被审查者选审查员。
        policy_name = self._routes.get(value, self._fallback) if value else self._fallback
        if policy_name not in self._policies:
            policy_name = self._fallback
        return self._policies[policy_name]


def _route_value(resource: ResourceDescriptor, route_key: str) -> str:
    if route_key == "resource_type":
        return resource.resource_type
    return str(resource.attributes.get(route_key, "")).strip()


@AuthorizationProducer.register("routing")
def _build(config) -> RoutingAuthorizer:
    route_key = config.get("route_key", "memory_type")
    fallback = str(config.get("fallback", "")).strip()
    if not fallback:
        raise ValidationError("authorizer.routing params.fallback 必须指向一个具名 authorizer")
    if fallback == config.name:
        raise ValidationError("authorizer.routing params.fallback 不能指向 routing 自身")
    routes_raw = config.get("routes", {})
    if not isinstance(routes_raw, dict):
        raise ValidationError("authorizer.routing params.routes 必须是映射")
    routes = {str(key): str(value) for key, value in routes_raw.items()}
    if any(policy_name == config.name for policy_name in routes.values()):
        raise ValidationError("authorizer.routing params.routes 不能指向 routing 自身")
    policy_names = set(routes.values()) | {fallback}
    policies: dict[str, Authorizer] = {}
    for policy_name in policy_names:
        policy = AuthorizationProducer.build_named(policy_name, config.ctx)
        if not isinstance(policy, Authorizer):
            raise ValidationError(f"authorizer.routing 的 {policy_name!r} 必须是 Authorizer")
        policies[policy_name] = policy
    return RoutingAuthorizer(
        policies=policies,
        routes=routes,
        fallback=fallback,
        route_key=route_key,
    )
