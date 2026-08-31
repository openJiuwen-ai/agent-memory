"""build_kernel 的 config 驱动装配：用户配置合并覆盖到内置默认之上，各组件经引用取依赖。

验证：默认（无 config）走离线进程内缺省；config 覆盖某具名实例的 target 时改用该实现；未注册的
target 在 build 阶段报错；顶层段名拼错在解析期报错；具名实例经 ``build_named`` 共享单例。
以控制层 permission + 存储层 vector_store 作可观测点。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.api.memory_api_impl import assembly, build_kernel
from jiuwen_memory.common.audit.base import AuditProducer
from jiuwen_memory.common.errors import BackendError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.audit_integrity.base import AuditVerificationLimits
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.security_impl.local_envelope_security_provider import (
    LocalEnvelopeSecurityProvider,
)
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.construction.router import RouterProducer, optional_router
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.permission import PermissionManager, PermissionProducer
from jiuwen_memory.control.types import Action, Grant, PermissionContext
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.vector import VectorProducer

SCOPE = Scope(org="o", user="u")

_VEC_BUILT: list = []


@VectorProducer.register("counting_test")
def _build_counting_vector(config):
    from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

    store = InMemoryVectorStore()
    _VEC_BUILT.append(store)
    return store


class _DenyAllPermission(PermissionManager):
    """测试用：check 恒拒绝。"""

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PERMISSION

    def health(self) -> None:
        return None

    def grant(self, grant: Grant) -> None:  # pragma: no cover - 测试不触发
        ...

    def revoke(self, grant: Grant) -> None:  # pragma: no cover
        ...

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        return False


@PermissionProducer.register("deny_all_test")
def _build_deny(config) -> _DenyAllPermission:
    return _DenyAllPermission()


_ROUTERS_BUILT: list = []


@RouterProducer.register("counting_router_test")
def _build_counting_router(config):
    router = object()
    _ROUTERS_BUILT.append(router)
    return router


def test_default_assembly_allows_write() -> None:
    """无 config：内置默认 owner-only sqlite ACL，owner 写入放行、可召回。"""
    api = assemble()
    units = api.add("hello", SCOPE, security=legacy_request_context(SCOPE))
    assert units and api.search(
        "hello", Context(SCOPE), security=legacy_request_context(SCOPE)
    ).items


def test_default_audit_config_uses_in_memory_sqlite() -> None:
    audit_config = default_config_dict()["audit"]["default"]
    api = assemble()
    api.add("audit default smoke", SCOPE, security=legacy_request_context(SCOPE))

    assert audit_config == {"target": "sqlite", "params": {"db_path": ":memory:"}}
    events = api.audit({"action": "add"}, security=legacy_request_context(Scope()))
    assert any(event.action == "add" for event in events)


def test_audit_verify_limits_from_globals_reach_pep(monkeypatch) -> None:
    """globals 是唯一配置入口；合法整数必须实际注入 LocalMemoryAPI。"""
    captured: dict[str, AuditVerificationLimits] = {}
    local_memory_api = assembly.LocalMemoryAPI

    def _capture_limits(*args, **kwargs):
        captured["limits"] = kwargs["audit_verify_limits"]
        return local_memory_api(*args, **kwargs)

    monkeypatch.setattr(assembly, "LocalMemoryAPI", _capture_limits)
    cfg = Config.from_dict(
        {
            "globals": {
                "audit_verify_max_page_size": 2000,
                "audit_verify_max_samples": 50,
            }
        }
    )

    assemble(config=cfg)

    assert captured["limits"] == AuditVerificationLimits(
        max_page_size=2000,
        max_samples=50,
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("audit_verify_max_page_size", 20_000, "hard limit 10000"),
        ("audit_verify_max_samples", 101, "hard limit 100"),
        ("audit_verify_max_page_size", "2000", "must be an integer"),
        ("audit_verify_max_page_size", "two thousand", "must be an integer"),
        ("audit_verify_max_samples", True, "must be an integer"),
    ],
)
def test_invalid_audit_verify_globals_raise_validation_error(
    key: str,
    value: object,
    message: str,
) -> None:
    cfg = Config.from_dict({"globals": {key: value}})

    with pytest.raises(ValidationError, match=message):
        assemble(config=cfg)


def test_assembly_audit_fallback_matches_sqlite_default(monkeypatch) -> None:
    seen_defaults = []
    original_dep = AuditProducer.dep

    def capture_dep(_cls, config, param_name=None, default=None):
        seen_defaults.append(default)
        return original_dep(config, param_name, default)

    root_params = dict(assembly.ROOT_PARAMS)
    root_params.pop("audit")
    monkeypatch.setattr(assembly, "ROOT_PARAMS", root_params)
    monkeypatch.setattr(AuditProducer, "dep", classmethod(capture_dep))

    assembly.assemble()

    assert seen_defaults
    assert set(seen_defaults) == {"sqlite"}


def test_config_overrides_control_operator() -> None:
    """覆盖 permission.default=deny_all_test → 合并到默认之上，写入被拒。"""
    cfg = Config.from_dict({"permission": {"default": "deny_all_test"}})
    api = assemble(config=cfg)
    with pytest.raises(PermissionDeniedError):
        api.add("hello", SCOPE, security=legacy_request_context(SCOPE))


def test_unknown_operator_target_raises() -> None:
    """config 指定未注册的算子 target → build 阶段（递归到该 Producer）报 ValidationError。"""
    cfg = Config.from_dict({"fuser": {"default": "nope"}})
    with pytest.raises(ValidationError):
        assemble(config=cfg)


def test_unknown_top_name_raises() -> None:
    """顶层段名拼错（非任何 Producer 的 TOP_NAME）→ 解析期校验报错。"""
    cfg = Config.from_dict({"vectorstore": {"default": "memory"}})
    with pytest.raises(ValidationError):
        assemble(config=cfg)


def test_named_instance_built_via_its_producer() -> None:
    """具名实例经其 Producer 的 build_named 产出。"""
    Factory.reset_all()
    _VEC_BUILT.clear()
    ctx = AssemblyContext.from_dict({"vector_store": {"main": "counting_test"}})
    store = VectorProducer.build_named("main", ctx)
    assert store in _VEC_BUILT


def test_build_named_shares_single_instance() -> None:
    """同名具名实例只建一次、共享单例（如 recaller 与 index_builder 引用同名 vector_store）。"""
    Factory.reset_all()
    _VEC_BUILT.clear()
    ctx = AssemblyContext.from_dict({"vector_store": {"main": "counting_test"}})
    a = VectorProducer.build_named("main", ctx)
    b = VectorProducer.build_named("main", ctx)
    assert a is b
    assert len(_VEC_BUILT) == 1


def test_optional_router_shares_by_name_but_not_by_inline_config() -> None:
    """判定算子跨消费方是否同实例，取决于配置写法。

    三个消费方（API 层与两个 Evolver）各自调 ``optional_router``。经 ``router.default``
    具名引用时 ``Factory.build_named`` 缓存，三方拿到同一实例、共用一份判定表；而组件
    配置里内联 ``router`` 参数时走 ``dep`` 的匿名分支，每次调用新建一个实例，两侧判定表
    可以不同——此时写入边界拒绝的键集合按 API 层的表算、实际落点按构建层的表算。

    本用例把这两种写法的差别钉在测试里。分歧本身是已接受的遗留（F07「已知遗留 > 本期
    不做」的判定表跨实例一致一项），此处保证它不会在无人察觉时改变方向。
    """
    Factory.reset_all()
    _ROUTERS_BUILT.clear()
    ctx = AssemblyContext.from_dict({"router": {"default": "counting_router_test"}})

    named = ComponentConfig(params={}, ctx=ctx, target="", name="")
    assert optional_router(named) is optional_router(named)
    assert len(_ROUTERS_BUILT) == 1

    _ROUTERS_BUILT.clear()
    inline = ComponentConfig(
        params={"router": {"target": "counting_router_test", "params": {}}},
        ctx=ctx,
        target="",
        name="",
    )
    assert optional_router(inline) is not optional_router(inline)
    assert len(_ROUTERS_BUILT) == 2


def test_optional_router_returns_none_without_the_router_namespace() -> None:
    """未声明 router 命名空间即不装配：判定表为空、判定路径整体不可达。"""
    Factory.reset_all()
    config = ComponentConfig(params={}, ctx=AssemblyContext.from_dict({}), target="", name="")
    assert optional_router(config) is None


def test_default_assembly_does_not_wrap_kv() -> None:
    """F04 §5.4：默认装配不强制包装 EncryptedKVStore。"""
    kernel = build_kernel()
    assert not isinstance(kernel.kv, EncryptedKVStore)


def test_security_namespace_params_apply_on_encrypted_kv_target() -> None:
    """security.default.params 经 opt-in encrypted KV target 生效（allow_plaintext/key_hex）。"""
    key_hex = "a" * 64
    cfg = Config.from_dict(
        {
            "security": {
                "default": {
                    "target": "local",
                    "params": {
                        "allow_plaintext": False,
                        "key_hex": key_hex,
                        "create_key_file": False,
                    },
                }
            },
            "kv_store": {
                "raw": {"target": "sqlite", "params": {"db_path": ":memory:"}},
                "default": {
                    "target": "encrypted",
                    "params": {"raw_kv_store": "raw", "security": "default"},
                },
            },
        }
    )
    kernel = build_kernel(config=cfg)
    security = getattr(kernel.kv, "_security")
    assert isinstance(security, LocalEnvelopeSecurityProvider)
    assert getattr(security, "_allow_plaintext") is False
    assert getattr(getattr(security, "_key_provider"), "_key_hex") == key_hex

    getattr(kernel.kv, "_raw").insert(SCOPE, "plain_key", b"hello-plaintext")
    with pytest.raises(BackendError):
        kernel.kv.get(SCOPE, "plain_key")
