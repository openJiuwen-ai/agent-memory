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
from jiuwen_memory.common.security.security_impl.local_envelope_security_provider import (
    LocalEnvelopeSecurityProvider,
)
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_config_dict
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


def test_default_assembly_allows_write() -> None:
    """无 config：内置默认 owner-only sqlite ACL，owner 写入放行、可召回。"""
    api = assemble()
    units = api.add("hello", SCOPE, identity=SCOPE)
    assert units and api.search("hello", Context(SCOPE), identity=SCOPE).items


def test_default_audit_config_uses_in_memory_sqlite() -> None:
    audit_config = default_config_dict()["audit"]["default"]
    api = assemble()
    api.add("audit default smoke", SCOPE, identity=SCOPE)

    assert audit_config == {"target": "sqlite", "params": {"db_path": ":memory:"}}
    events = api.audit({"action": "add"}, identity=Scope())
    assert any(event.action == "add" for event in events)


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
        api.add("hello", SCOPE, identity=SCOPE)


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
