"""RoutingAuthorizer：只回答「这次判定归谁管」，以及它的三条装配守卫。

本实现**不定义任何授权语义**，因此这里不测 truth table（那是
``test_standard_authorizer.py`` 的事）。这里测两件事：

1. **路由选择**——尤其是「未命中一律落 fallback」而不是「路由值本身当策略名用」。
   后者会让调用方点名挑选审查自己的策略，等于让被审查者选审查员。
2. **装配守卫**——三条都是「拒绝启动」型的，不测就等于没有保证它们还在：
   fallback 必须存在、fallback 不得恒放行、全部 policy 必须共享同一 GrantStore。

判定桩用记录型假件而不是真 ``StandardAuthorizer``：路由测试要能一眼看出「哪个
policy 被调到了」，真判定实现会让断言变成「结果恰好不同」，路由错了但两条策略
判定相同时测不出来。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.authorization.authorization_impl.routing_authorizer import (
    RoutingAuthorizer,
)
from jiuwen_memory.common.security.authorization.base import (
    AuthorizationDecision,
    Authorizer,
)
from jiuwen_memory.common.security.authorization.store import GrantStore
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    AuthorizationEnvironment,
    DenyReason,
    ResourceDescriptor,
)
from jiuwen_memory.common.type_def import Scope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
ALICE = Scope(org="acme", space="main", user="alice")


# ====================================================================== #
# 假件
# ====================================================================== #


class FakeGrantStore(GrantStore):
    """只用来做实例同一性判断——真源统一守卫按 ``id(store)`` 比对。"""

    def add(self, grant) -> None:
        return None

    def revoke(self, grant_id: str) -> None:
        return None

    def find_active(self, *, grantee, grantor_org, action, now):
        return []

    def health(self) -> None:
        return None


class RecordingAuthorizer(Authorizer):
    """记录自己是否被调到的判定桩。

    ``decision`` 可控，好让「路由透传原样返回」与「路由不额外加语义」分开断言。
    """

    def __init__(
        self,
        name: str,
        *,
        store: GrantStore | None = None,
        test_only: bool = False,
        healthy: bool = True,
        decision: AuthorizationDecision | None = None,
    ) -> None:
        self.name = name
        self.calls = 0
        self._store = store
        self._test_only = test_only
        self._healthy = healthy
        self._decision = decision or AuthorizationDecision.allow(f"stub_{name}")

    def authorize(self, *, auth, resource, environment) -> AuthorizationDecision:
        self.calls += 1
        return self._decision

    def is_test_only(self) -> bool:
        return self._test_only

    def management_grant_store(self) -> GrantStore | None:
        return self._store

    def health(self) -> None:
        if not self._healthy:
            raise RuntimeError(f"{self.name} unhealthy")


def _resource(
    *,
    resource_type: str = "memory_unit",
    attributes: dict[str, str] | None = None,
) -> ResourceDescriptor:
    return ResourceDescriptor(
        action=Action.READ,
        resource_type=resource_type,
        scope=ALICE,
        attributes=attributes or {},
    )


def _decide(authorizer: RoutingAuthorizer, resource: ResourceDescriptor):
    return authorizer.authorize(
        auth=AuthContext(actor=ALICE),
        resource=resource,
        environment=AuthorizationEnvironment(now=NOW),
    )


def _routing(**overrides) -> tuple[RoutingAuthorizer, dict[str, RecordingAuthorizer]]:
    """默认拓扑：episodic → strict，其余落 lenient（fallback）。"""
    policies: dict[str, RecordingAuthorizer] = {
        "strict": RecordingAuthorizer("strict"),
        "lenient": RecordingAuthorizer("lenient"),
    }
    policies.update(overrides.pop("policies", {}))
    routing = RoutingAuthorizer(
        policies=policies,
        routes=overrides.pop("routes", {"episodic": "strict"}),
        fallback=overrides.pop("fallback", "lenient"),
        **overrides,
    )
    return routing, policies


# ====================================================================== #
# 路由选择
# ====================================================================== #


def test_declared_route_value_selects_that_policy() -> None:
    """routes 里显式声明的值命中对应 policy。"""
    routing, policies = _routing()

    _decide(routing, _resource(attributes={"memory_type": "episodic"}))

    assert policies["strict"].calls == 1
    assert policies["lenient"].calls == 0


def test_undeclared_route_value_falls_back() -> None:
    """未在 routes 里声明的值落 fallback，不报错也不跳过判定。"""
    routing, policies = _routing()

    _decide(routing, _resource(attributes={"memory_type": "semantic"}))

    assert policies["lenient"].calls == 1
    assert policies["strict"].calls == 0


def test_route_value_naming_a_policy_is_not_a_shortcut() -> None:
    """路由值恰好等于某个 policy 名，但不在 routes 里 → 仍落 fallback。

    这条是本文件最重要的一条。Pipeline 路由（S03:136）允许「路由值本身是 profile 名
    则直接使用」，授权侧**刻意不沿用**：``memory_type`` 是调用方能影响的资源属性，
    若它能直接点名 policy，调用方就可以挑选审查自己的那一个——让被审查者选审查员。

    断言的是 ``strict`` **没被调到**：它是个合法 policy 名，routes 里却没有指向它的
    路由值，所以 ``memory_type: "strict"`` 必须与任何无效值一样落 fallback。
    """
    routing, policies = _routing()

    _decide(routing, _resource(attributes={"memory_type": "strict"}))

    assert policies["strict"].calls == 0
    assert policies["lenient"].calls == 1


def test_missing_route_value_falls_back() -> None:
    """资源属性里根本没有 route_key → fallback。

    这正是 fallback 不得恒放行的原因：调用方只要不声明类型就能走到这里。
    """
    routing, policies = _routing()

    _decide(routing, _resource(attributes={}))

    assert policies["lenient"].calls == 1


def test_blank_route_value_is_treated_as_missing() -> None:
    """全空白的路由值当作缺失，不去 routes 里查一个空串键。"""
    routing, policies = _routing(routes={"episodic": "strict", "": "strict"})

    _decide(routing, _resource(attributes={"memory_type": "   "}))

    assert policies["strict"].calls == 0, "空白值不得命中空串路由"
    assert policies["lenient"].calls == 1


def test_route_key_resource_type_reads_the_field_not_attributes() -> None:
    """``route_key="resource_type"`` 读 descriptor 的字段，不读 attributes。

    字段由 PEP 按操作确定，attributes 才是从真源读出的资源事实——两者取值路径不同，
    配错会让路由静默恒落 fallback。
    """
    routing, policies = _routing(
        routes={"job": "strict"},
        route_key="resource_type",
    )

    _decide(routing, _resource(resource_type="job", attributes={"resource_type": "memory_unit"}))

    assert policies["strict"].calls == 1


def test_route_pointing_at_an_unknown_policy_falls_back() -> None:
    """routes 指向未装配的 policy 名时落 fallback，而不是 KeyError。

    装配期 ``_build`` 会把 routes 里的每个名字都建出来，所以这条在正常装配下不可达；
    它守的是直接构造的调用方——落 fallback 是最小权限侧。
    """
    routing, policies = _routing(routes={"episodic": "nonexistent"})

    _decide(routing, _resource(attributes={"memory_type": "episodic"}))

    assert policies["lenient"].calls == 1


# ====================================================================== #
# 装配守卫 1：fallback 必须存在
# ====================================================================== #


def test_fallback_must_exist_in_policies() -> None:
    policies = {"strict": RecordingAuthorizer("strict")}
    with pytest.raises(ValidationError, match="不存在"):
        RoutingAuthorizer(policies=policies, routes={}, fallback="lenient")


# ====================================================================== #
# 装配守卫 2：fallback 不得恒放行
# ====================================================================== #


def test_test_only_fallback_is_rejected() -> None:
    """fallback 承接「不声明类型」的请求，恒放行的 fallback 等于免鉴权后门。"""
    policies = {
        "strict": RecordingAuthorizer("strict"),
        "wide_open": RecordingAuthorizer("wide_open", test_only=True),
    }
    with pytest.raises(ValidationError, match="不得是仅测试实现"):
        RoutingAuthorizer(policies=policies, routes={}, fallback="wide_open")


def test_test_only_fallback_is_rejected_by_capability_not_by_name() -> None:
    """判据是 ``is_test_only()`` 而不是 target 名（S08 不变量 7）。

    这里的桩叫 ``prod_looking_policy``——核心不认识第三方注册的 target 名，只能问
    capability。改成按名字判，这条就会漏掉。
    """
    policies = {"prod_looking_policy": RecordingAuthorizer("prod_looking_policy", test_only=True)}
    with pytest.raises(ValidationError, match="不得是仅测试实现"):
        RoutingAuthorizer(policies=policies, routes={}, fallback="prod_looking_policy")


def test_test_only_policy_behind_an_explicit_route_propagates() -> None:
    """非 fallback 位置的恒放行 policy 也让整条路由 test-only（审核 P1-3）。

    装配守卫 ``assembly._reject_test_only_authorizer`` 问的是最终 authorizer 的
    ``is_test_only()``，只能看见外层——不传播的话，嵌在某条显式路由后面的恒放行
    delegate 藏在路由壳后面进了生产：请求带上对应路由值即恒放行。修复后由
    ``is_test_only()`` 传播任一可达 delegate 的属性，装配层按既有判据拦截。
    """
    policies = {
        "lenient": RecordingAuthorizer("lenient"),
        "wide_open": RecordingAuthorizer("wide_open", test_only=True),
    }
    routing = RoutingAuthorizer(
        policies=policies, routes={"scratch": "wide_open"}, fallback="lenient"
    )

    assert routing.is_test_only() is True


def test_production_assembly_rejects_test_only_policy_behind_a_route() -> None:
    """端到端：配了 ``security`` 段的生产装配，路由里藏 allow_all → 拒绝启动。

    这是 P1-3 的验收断言：装配守卫经传播后的 capability 拦住路由壳后面的恒放行
    delegate。没配 ``security`` 段的测试装配不受影响（同守卫的既有判据）。

    ``build_named`` 的具名缓存不带 ctx（具名单例设计）：守卫虽在装配中途抛错，
    test-only 的 routing 实例已进 ``AuthorizationProducer._instances["default"]``。
    前后都 ``reset_all``——开头隔离前序污染，结尾不让它泄给同进程的其他测试
    （否则 ``test_runtime`` 默认装配会命中缓存拿到本测试的 routing 实例）。
    """
    from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.config import Config

    config = Config.from_dict(
        {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {"authenticator": "default"},
                }
            },
            "authenticator": {"default": {"target": "api_key", "params": {"key_store": "default"}}},
            "key_store": {"default": {"target": "memory"}},
            "authorizer": {
                "default": {
                    "target": "routing",
                    "params": {
                        "fallback": "standard",
                        "routes": {"scratch": "wide_open"},
                    },
                },
                "wide_open": {"target": "allow_all"},
                "standard": {
                    "target": "standard",
                    "params": {
                        "grant_store": "default",
                        "delegation_store": "default",
                    },
                },
            },
            "grant_store": {"default": {"target": "memory"}},
            "delegation_store": {"default": {"target": "memory"}},
        }
    )
    Factory.reset_all()
    try:
        with pytest.raises(ValidationError, match="仅限测试"):
            build_kernel(config=config)
    finally:
        Factory.reset_all()


# ====================================================================== #
# 装配守卫 3：全部 policy 共享同一 GrantStore
# ====================================================================== #


def test_policies_with_different_grant_stores_are_rejected() -> None:
    """两个 Store 就是两个真源：grant 顺序写入无原子性，第二个失败即部分授权。"""
    policies = {
        "strict": RecordingAuthorizer("strict", store=FakeGrantStore()),
        "lenient": RecordingAuthorizer("lenient", store=FakeGrantStore()),
    }
    with pytest.raises(ValidationError, match="必须共享同一个 GrantStore"):
        RoutingAuthorizer(policies=policies, routes={}, fallback="lenient")


def test_policies_sharing_one_store_are_accepted() -> None:
    shared = FakeGrantStore()
    policies = {
        "strict": RecordingAuthorizer("strict", store=shared),
        "lenient": RecordingAuthorizer("lenient", store=shared),
    }
    routing = RoutingAuthorizer(policies=policies, routes={}, fallback="lenient")

    assert routing.management_grant_stores() == [shared]


def test_policies_without_stores_are_accepted() -> None:
    """无 GrantStore 的实现不触发真源统一检查——没有真源就无所谓分叉。"""
    routing, _ = _routing()
    assert routing.management_grant_stores() == []


def test_management_grant_stores_returns_every_store_deduped() -> None:
    """公共 grant 要写入全部 Store（P1-4），否则「按 A 判定授权、按 B 判定访问」读不到。

    三个 policy 共享同一 Store 的拓扑：返回值必须是**一个** Store，不是三个引用。
    """
    shared = FakeGrantStore()
    policies = {
        "a": RecordingAuthorizer("a", store=shared),
        "b": RecordingAuthorizer("b", store=shared),
        "c": RecordingAuthorizer("c", store=shared),
    }
    routing = RoutingAuthorizer(policies=policies, routes={}, fallback="a")

    assert routing.management_grant_stores() == [shared]


def test_management_grant_store_transparently_reads_the_fallback() -> None:
    """已弃用的单 Store 读法透传 fallback 的 Store，向下兼容不变。"""
    shared = FakeGrantStore()
    policies = {
        "strict": RecordingAuthorizer("strict", store=shared),
        "lenient": RecordingAuthorizer("lenient", store=shared),
    }
    routing = RoutingAuthorizer(policies=policies, routes={}, fallback="lenient")

    assert routing.management_grant_store() is shared


# ====================================================================== #
# 透传：路由不添加任何授权语义
# ====================================================================== #


def test_selected_policy_decision_is_returned_verbatim() -> None:
    """被选中 policy 的拒绝原样返回——路由不改 reason，审计才对得上。"""
    denied = AuthorizationDecision.deny(DenyReason.CROSS_ORG, "org_boundary")
    policies = {
        "strict": RecordingAuthorizer("strict", decision=denied),
        "lenient": RecordingAuthorizer("lenient"),
    }
    routing = RoutingAuthorizer(
        policies=policies, routes={"episodic": "strict"}, fallback="lenient"
    )

    decision = _decide(routing, _resource(attributes={"memory_type": "episodic"}))

    assert not decision.allowed
    assert decision.reason is DenyReason.CROSS_ORG
    assert decision.rule == "org_boundary"


def test_only_the_selected_policy_is_consulted() -> None:
    """不对多个 policy 求交集：选中谁就只问谁。

    求交集会让「加一条更宽松的路由」意外收紧另一条路由的判定，且拒绝原因来自一个
    根本不该管这次请求的策略。
    """
    routing, policies = _routing()

    _decide(routing, _resource(attributes={"memory_type": "episodic"}))

    assert policies["strict"].calls == 1
    assert policies["lenient"].calls == 0


def test_routing_fields_declares_the_route_key() -> None:
    """PEP 靠它把路由值回注为系统谓词，绑定「按哪条策略授权」与「能读到哪些数据」。

    返回空元组会让 PEP 不回注谓词——「用 A 策略授权、读 B 类型数据」就成立了。
    """
    routing, _ = _routing(route_key="memory_type")
    assert routing.routing_fields() == ("memory_type",)


def test_routing_itself_is_not_test_only() -> None:
    """路由实现本身可以进生产装配；恒放行与否取决于它的 delegate。"""
    routing, _ = _routing()
    assert routing.is_test_only() is False


# ====================================================================== #
# health
# ====================================================================== #


def test_health_probes_every_policy() -> None:
    """任一 delegate 不健康就抛——包括当前没有路由指向的那些。

    只探 fallback 会让「配了但暂时没流量」的 policy 在第一个命中请求打进来时才暴露。
    """
    policies = {
        "lenient": RecordingAuthorizer("lenient"),
        "broken": RecordingAuthorizer("broken", healthy=False),
    }
    routing = RoutingAuthorizer(
        policies=policies, routes={"episodic": "broken"}, fallback="lenient"
    )

    with pytest.raises(RuntimeError, match="broken unhealthy"):
        routing.health()


def test_health_passes_when_every_policy_is_healthy() -> None:
    routing, _ = _routing()
    assert routing.health() is None
