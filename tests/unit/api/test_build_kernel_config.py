"""build_kernel 的 config 驱动装配：用户配置合并覆盖到内置默认之上，各组件经引用取依赖。

验证：默认（无 config）走离线进程内缺省；config 覆盖某具名实例的 target 时改用该实现；未注册的
target 在 build 阶段报错；顶层段名拼错在解析期报错；具名实例经 ``build_named`` 共享单例。
以安全层 authorizer + 存储层 vector_store 作可观测点——授权判定已从 ``control.permission``
迁到 ``common.security.authorization``，``permission`` 段现在只喂 grant/revoke 的记录通道，
覆盖它观测不到任何判定变化。
"""

from __future__ import annotations

import pytest

from api import assemble
from api.memory_api_impl import assembly
from common.audit.base import AuditProducer
from common.errors import PermissionDeniedError, ValidationError
from common.factory.factory import Factory
from common.security.authorization.base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
)
from common.security.types import (
    AuthContext,
    AuthorizationEnvironment,
    DenyReason,
    ResourceDescriptor,
)
from common.type_def import Context, Scope
from config import Config
from config.context import AssemblyContext
from config.defaults import default_config_dict
from storage.vector import VectorProducer
from tests.conftest import root_sec, sec

SCOPE = Scope(org="o", user="u")

_VEC_BUILT: list = []


@VectorProducer.register("counting_test")
def _build_counting_vector(config):
    from storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

    store = InMemoryVectorStore()
    _VEC_BUILT.append(store)
    return store


class _DenyAllAuthorizer(Authorizer):
    """测试用：恒拒绝。

    刻意**不**声明 ``is_test_only``——那个 capability 的含义是「恒放行、生产装配必须
    拒绝启动」。恒拒绝没有这个风险，若把它也标成 test-only，本用例就得额外打开
    ``allow_test_only_security``，反而弱化了那道闸门在别处的可信度。
    """

    def authorize(
        self,
        *,
        auth: AuthContext,
        resource: ResourceDescriptor,
        environment: AuthorizationEnvironment,
    ) -> AuthorizationDecision:
        return AuthorizationDecision.deny(DenyReason.DEFAULT_DENY, "deny_all_test")

    def health(self) -> None:
        return None


@AuthorizationProducer.register("deny_all_test")
def _build_deny(config) -> _DenyAllAuthorizer:
    return _DenyAllAuthorizer()


def test_default_assembly_allows_write() -> None:
    """无 config：内置默认 owner-only sqlite ACL，owner 写入放行、可召回。"""
    api = assemble()
    units = api.write("hello", SCOPE, security=sec(SCOPE))
    assert units and api.recall("hello", Context(SCOPE), security=sec(SCOPE)).items


def test_default_audit_config_uses_in_memory_sqlite() -> None:
    audit_config = default_config_dict()["audit"]["default"]
    api = assemble()
    api.write("audit default smoke", SCOPE, security=sec(SCOPE))

    assert audit_config == {"target": "sqlite", "params": {"db_path": ":memory:"}}
    events = api.audit({"action": "write"}, security=root_sec())
    assert any(event.action == "write" for event in events)


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


def test_config_overrides_security_component() -> None:
    """覆盖 authorizer.default=deny_all_test → 合并到默认之上，写入被拒。"""
    cfg = Config.from_dict({"authorizer": {"default": "deny_all_test"}})
    api = assemble(config=cfg)
    with pytest.raises(PermissionDeniedError):
        api.write("hello", SCOPE, security=sec(SCOPE))


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
