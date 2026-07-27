"""权限上下文路由：按 memory_type 选择 delegate，并把路由值绑定到可读数据范围。

S03 的两条契约在此同时生效——`:135` 查询侧路由取值 extensions 优先、其次等值 filter；
`:207` routing 只选择 delegate、不改变授权语义（root / owner-cover / Grant 全部由被选中
的 delegate 判定）。

由此产生一个结构性风险：**「谁来审查」和「能读到什么」是两个独立输入，且都由调用方
控制**。调用方可以把路由值填成宽松策略对应的类型、filters 却指向受严格策略保护的数据，
即「用 A 的钥匙开 B 的门」。两道防线：

1. 授权所依据的路由值被**回注为系统谓词**——按 memory_type=X 授的权就只能读到 X 的数据；
2. 路由值缺失时无值可注入，全部落 ``fallback``，因此 fallback 必须是**最小权限**策略
   （装配期强制，见 ``test_permissive_fallback_rejected_at_assembly``）。
"""

from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import Context, Scope
from config import Config
from control.types import DeleteMode, DeleteSelector

pytestmark = pytest.mark.unit


def _routing_config() -> Config:
    """coding 受 strict 保护、episodic 显式放宽；fallback 取最小权限。"""
    return Config.from_dict(
        {
            "permission": {
                "default": {
                    "target": "routing",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "strict",
                        "routes": {"coding": "strict", "episodic": "standard"},
                    },
                },
                "standard": "allow_all",
                "strict": "sqlite",
            }
        }
    )


def test_permissive_fallback_rejected_at_assembly() -> None:
    """fallback 承接路由值缺失的请求（调用方不写 filters 即可触发），不得是 allow_all。"""
    cfg = Config.from_dict(
        {
            "permission": {
                "default": {
                    "target": "routing",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "standard",
                        "routes": {"coding": "strict"},
                    },
                },
                "standard": "allow_all",
                "strict": "sqlite",
            }
        }
    )

    with pytest.raises(ValidationError):
        build_kernel(config=cfg)


def test_policy_name_is_not_accepted_as_route_value() -> None:
    """路由值只认 ``routes`` 里显式声明的键；直接点名 policy 等于让被审查者挑审查员。

    与 Pipeline 路由不同——S03:136 允许「路由值本身是 profile 名则直接使用」，授权侧
    不能沿用该兜底。
    """
    api = build_kernel(config=_routing_config()).api
    outsider, victim = Scope(org="evil", user="x"), Scope(org="acme", user="owner")

    # episodic 是显式声明的宽松路由，standard 只是它背后的 policy 名
    api.write("ok", victim, identity=outsider, metadata={"memory_type": "episodic"})

    with pytest.raises(PermissionDeniedError):
        api.write("secret", victim, identity=outsider, metadata={"memory_type": "standard"})


def test_write_permission_routes_by_memory_type() -> None:
    api = build_kernel(config=_routing_config()).api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    api.write("general note", target, identity=actor, metadata={"memory_type": "episodic"})

    with pytest.raises(PermissionDeniedError):
        api.write(
            "repo must use pytest",
            target,
            identity=actor,
            metadata={"memory_type": "coding"},
        )


def test_recall_permission_routes_by_metadata_memory_type_filter() -> None:
    api = build_kernel(config=_routing_config()).api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    with pytest.raises(PermissionDeniedError):
        api.recall(
            "repo",
            Context(scope=target),
            identity=actor,
            filters={"metadata.memory_type": "coding"},
        )


def test_recall_permission_routes_to_lenient_policy_for_declared_type() -> None:
    """显式声明为宽松策略的类型照常放行（routing 只选 delegate，不额外加码）。"""
    api = build_kernel(config=_routing_config()).api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    api.recall(
        "general",
        Context(scope=target),
        identity=actor,
        filters={"metadata.memory_type": "episodic"},
    )


# -- 四条越权路径（均实测复现过，必须保持关闭）-------------------------------- #


def _seed(api, owner: Scope) -> None:
    api.write("repo must use pytest", owner, identity=owner, metadata={"memory_type": "coding"})


def test_escalation_1_unknown_extensions_value_falls_to_strict_fallback() -> None:
    """路由值指向未定义的类型 → 落 fallback；fallback 是最小权限，故拒绝。"""
    api = build_kernel(config=_routing_config()).api
    owner, reader = Scope(org="acme", user="owner"), Scope(org="acme", user="reader")
    _seed(api, owner)

    with pytest.raises(PermissionDeniedError):
        api.recall(
            "repo must use pytest",
            Context(scope=owner, extensions={"memory_type": "unknown"}),
            identity=reader,
            filters={"metadata.memory_type": "coding"},
        )


def test_escalation_2_missing_route_value_falls_to_strict_fallback() -> None:
    """不带任何 filters → 无路由值可注入 → 落 fallback（最小权限）。"""
    api = build_kernel(config=_routing_config()).api
    owner, reader = Scope(org="acme", user="owner"), Scope(org="acme", user="reader")
    _seed(api, owner)

    with pytest.raises(PermissionDeniedError):
        api.recall("repo must use pytest", Context(scope=owner), identity=reader)


def test_escalation_3_ambiguous_or_filter_falls_to_strict_fallback() -> None:
    """OR 歧义过滤推不出唯一等值 → 落 fallback。树形表达式带来的新攻击面。"""
    api = build_kernel(config=_routing_config()).api
    owner, reader = Scope(org="acme", user="owner"), Scope(org="acme", user="reader")
    _seed(api, owner)

    with pytest.raises(PermissionDeniedError):
        api.recall(
            "repo must use pytest",
            Context(scope=owner),
            identity=reader,
            filters={
                "OR": [
                    {"metadata.memory_type": "coding"},
                    {"metadata.memory_type": "general"},
                ]
            },
        )


def test_escalation_4_lenient_route_cannot_read_protected_data() -> None:
    """路由到宽松具名策略可以通过鉴权，但**读不到**受严格策略保护的数据。

    这条不经过 fallback，所以「fallback 最小权限」挡不住——靠的是把路由值回注为
    系统谓词：``memory_type=episodic`` 与用户的 ``coding`` 过滤 AND 后自相矛盾。
    """
    api = build_kernel(config=_routing_config()).api
    owner, reader = Scope(org="acme", user="owner"), Scope(org="acme", user="reader")
    _seed(api, owner)

    result = api.recall(
        "repo must use pytest",
        Context(scope=owner, extensions={"memory_type": "episodic"}),
        identity=reader,
        filters={"metadata.memory_type": "coding"},
        top_k=10,
    )

    assert result.items == [], "按 episodic 授的权不得读到 coding 数据"


def test_route_value_injection_still_returns_own_type_data() -> None:
    """回注谓词不得误伤：按 episodic 授权时，episodic 的数据必须照常可读。"""
    api = build_kernel(config=_routing_config()).api
    owner, reader = Scope(org="acme", user="owner"), Scope(org="acme", user="reader")
    api.write("lunch plan tomorrow", owner, identity=owner, metadata={"memory_type": "episodic"})

    result = api.recall(
        "lunch plan tomorrow",
        Context(scope=owner, extensions={"memory_type": "episodic"}),
        identity=reader,
        top_k=10,
    )

    assert [item.content for item in result.items] == ["lunch plan tomorrow"]


# -- delegate 的基础规则必须保留（S03:207）------------------------------------ #


def test_unresolved_route_keeps_owner_base_rule() -> None:
    """路由未解析时落 fallback，owner-cover 等基础规则由该 delegate 正常判定。

    曾经在 delegate.check 之前就 ``return False``，导致 owner 查自己 scope 也被拒。
    """
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")

    api.recall("general", Context(scope=owner), identity=owner)  # 未限定 memory_type


def test_unresolved_route_keeps_root_base_rule() -> None:
    api = build_kernel(config=_routing_config()).api
    owner, root = Scope(org="acme", user="owner"), Scope()

    api.recall("general", Context(scope=owner), identity=root)


# -- 已有 unit 的操作按真源元数据鉴权 ------------------------------------------ #


def test_get_permission_uses_stored_memory_type_context() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    unit = api.write(
        "repo must use pytest",
        owner,
        identity=owner,
        metadata={"memory_type": "coding"},
    )[0]

    with pytest.raises(PermissionDeniedError):
        api.get(unit.id, owner, identity=reader)


def test_delete_permission_checks_each_matched_unit_context() -> None:
    api = build_kernel(config=_routing_config()).api
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    unit = api.write(
        "repo must use pytest",
        owner,
        identity=owner,
        tags=["repo"],
        metadata={"memory_type": "coding"},
    )[0]

    with pytest.raises(PermissionDeniedError):
        api.delete(
            DeleteSelector(unit_ids=[unit.id], scope=owner, mode=DeleteMode.FORGET),
            identity=reader,
        )
