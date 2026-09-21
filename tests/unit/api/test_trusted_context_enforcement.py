"""可信上下文校验必须覆盖**每一个**公开入口（审核 P0-1 的回归防线）。

PR2 曾存在两条旁路：跨空间 ``search``（``extensions["spaces"]``）不经
``_authorize_with_context`` 直接进 ``_search_spaces``；``list_spaces`` 在空间感知装配下
走 ``check_permission=False`` 早退，而早退分支排在可信性校验**之前**。两者都只做了
actor 形态检查与 ``_perm.decide``——伪造来源的上下文（``has_valid_origin()`` 为假）
可以自述一个 actor 直接拿到读取结果。

修复后两条入口与常规入口同一道关。本文件按审核要求覆盖四组上下文不可信的形态
（伪造来源 / 已过期 / 已篡改 / 凭据已撤销），并断言 Engine 与 SpaceManager **未被调用**——
拒绝必须发生在任何业务组件之前，否则拒绝的请求仍可能留下副作用。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from inspect import Parameter, isawaitable, signature

import pytest

from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.authentication.credential_registry import (
    CredentialStatusRegistry,
)
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    fingerprint,
)
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    Grant,
    RequestSecurityContext,
    Role,
)
from jiuwen_memory.common.type_def import Context, Modality, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control import (
    BatchWriteItem,
    DeleteSelector,
    MemoryPatch,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
)
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

ORG = "acme"
VICTIM = "u-victim"
VICTIM_SCOPE = Scope(org=ORG, space=VICTIM)
ALICE = Scope(org=ORG, user="alice")
MALLORY = Scope(org=ORG, user="mallory")
OPS = Scope(org=ORG, user="ops")

_ENGINE_COMPONENT_NAMES = (
    "ingestor",
    "index_builder",
    "retriever",
    "kv_store",
    "scheduler",
    "evolver",
    "lifecycle",
)

# pylint: disable=protected-access  # 断言「业务组件未被调用」需要替换内部装配的入口


@pytest.fixture(scope="module")
def api():
    """cloud 引擎 + 空间感知判定：两条旁路只在空间感知装配下成立。"""
    kernel = build_kernel(
        config=Config.from_dict(
            {
                "engine": {
                    "default": {
                        "target": "cloud",
                        "params": {n: "default" for n in _ENGINE_COMPONENT_NAMES},
                    }
                },
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "authorizer": {
                    "default": {
                        "target": "space_aware",
                        "params": {"delegate": "standard"},
                    },
                    "standard": {
                        "target": "standard",
                        "params": {"grant_store": "default", "delegation_store": "default"},
                    },
                },
                "grant_store": {"default": "memory"},
                "delegation_store": {"default": "memory"},
            }
        )
    )
    victim = kernel.api
    victim.create_space(
        SpaceSpec(org=ORG, space=VICTIM, owner=ALICE),
        security=internal_context(ScopedAuthenticator(OPS, role=Role.ADMIN)),
    )
    victim.add(
        "forged-context-secret", VICTIM_SCOPE, security=internal_context(ScopedAuthenticator(ALICE))
    )
    return victim


def _forged(actor: Scope = ALICE) -> RequestSecurityContext:
    """无来源绑定证明的上下文——``has_valid_origin()`` 为假，模拟 dataclasses 直接构造。"""
    return RequestSecurityContext(auth=AuthContext(actor=actor))


def _expired(actor: Scope = ALICE) -> RequestSecurityContext:
    """来源证明有效但已过期的上下文。"""
    from jiuwen_memory.common.security.types import Surface

    return new_request_context(
        AuthContext(
            actor=actor,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        surface=Surface.INTERNAL,
    )


def _revoked_with(store, actor: Scope = ALICE) -> RequestSecurityContext:
    """来源证明有效、但凭据已撤销且声明需在线复核的上下文。

    ``store`` 必须是挂进 registry 的那一个——复核查的是注册的真源，不是上下文自带的。
    """
    from jiuwen_memory.common.security.types import Surface

    key = store.issue(actor, Role.USER)
    store.revoke(fingerprint(key))
    return new_request_context(
        AuthContext(
            actor=actor,
            role=Role.USER,
            credential_type="api_key",
            credential_issuer="runtime:default",
            credential_id=fingerprint(key),
            credential_status_required=True,
        ),
        surface=Surface.INTERNAL,
    )


_SPACES_CTX = Context(scope=Scope(org=ORG), extensions={"spaces": [VICTIM]})


@pytest.fixture
def untouched(api, monkeypatch):
    """让任何业务组件触碰都炸：拒绝必须发生在 Engine / SpaceManager 之前。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 断言它不该被调到
        raise AssertionError("业务组件在可信性校验之前被调用")

    monkeypatch.setattr(api._engine, "recall", _boom)
    monkeypatch.setattr(api._space, "list", _boom)
    return api


# -- 伪造来源 ------------------------------------------------------------------ #


def test_forged_origin_cannot_search_across_spaces(untouched) -> None:
    with pytest.raises(PermissionDeniedError):
        untouched.search("forged-context-secret", _SPACES_CTX, security=_forged(), top_k=10)


def test_forged_origin_cannot_list_spaces(untouched) -> None:
    with pytest.raises(PermissionDeniedError):
        untouched.list_spaces(ORG, security=_forged())


# -- 已过期 -------------------------------------------------------------------- #


def test_expired_context_cannot_search_across_spaces(untouched) -> None:
    with pytest.raises(PermissionDeniedError):
        untouched.search("forged-context-secret", _SPACES_CTX, security=_expired(), top_k=10)


def test_expired_context_cannot_list_spaces(untouched) -> None:
    with pytest.raises(PermissionDeniedError):
        untouched.list_spaces(ORG, security=_expired())


# -- 凭据已撤销 ---------------------------------------------------------------- #


def test_revoked_credential_cannot_search_across_spaces(api, monkeypatch) -> None:
    # registry 是生产装配缺口（S09 §已知过渡缺口）：这里直接挂上真源，测「撤销 → 拒」
    # 这条判定本身。缺 registry 的 fail-closed 已由 test_identity_forgery_rejected 覆盖。
    from jiuwen_memory.config.context import AssemblyContext

    store = KeyStoreProducer.build("memory", {}, AssemblyContext())
    registry = CredentialStatusRegistry()
    registry.register("api_key", "runtime:default", store)
    monkeypatch.setattr(api, "_credentials", registry)

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("业务组件在可信性校验之前被调用")

    monkeypatch.setattr(api._engine, "recall", _boom)
    monkeypatch.setattr(api._space, "list", _boom)

    with pytest.raises(PermissionDeniedError):
        api.search("forged-context-secret", _SPACES_CTX, security=_revoked_with(store), top_k=10)


def test_revoked_credential_cannot_list_spaces(api, monkeypatch) -> None:
    from jiuwen_memory.config.context import AssemblyContext

    store = KeyStoreProducer.build("memory", {}, AssemblyContext())
    registry = CredentialStatusRegistry()
    registry.register("api_key", "runtime:default", store)
    monkeypatch.setattr(api, "_credentials", registry)

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("业务组件在可信性校验之前被调用")

    monkeypatch.setattr(api._engine, "recall", _boom)
    monkeypatch.setattr(api._space, "list", _boom)

    with pytest.raises(PermissionDeniedError):
        api.list_spaces(ORG, security=_revoked_with(store))


# -- 对照：可信上下文两条入口照常工作 -------------------------------------------- #


def test_trusted_context_still_searches_across_spaces(api) -> None:
    result = api.search(
        "forged-context-secret",
        _SPACES_CTX,
        security=internal_context(ScopedAuthenticator(ALICE)),
        top_k=10,
    )
    assert result.items


def test_trusted_context_still_lists_spaces(api) -> None:
    infos = api.list_spaces(ORG, security=internal_context(ScopedAuthenticator(ALICE)))
    assert [i.space for i in infos] == [VICTIM]


# -- P1-1：拒绝的路由写入零副作用 ------------------------------------------------ #


def _routing_kernel():
    """启用归属判定的装配：路由写入会走 _ensure_fallback_space（自动建空间）。"""
    import test_collective_routing as _tcr  # 同目录：借它的 keyword_stub 路由注册

    return build_kernel(
        config=Config.from_dict(
            {
                "engine": {
                    "default": {
                        "target": "cloud",
                        "params": {n: "default" for n in _ENGINE_COMPONENT_NAMES},
                    }
                },
                "permission": {
                    "default": {"target": "space_aware", "params": {"db_path": ":memory:"}}
                },
                "router": {"default": {"target": "keyword_stub", "params": dict(_tcr.ROUTE_TABLE)}},
                "authorizer": {
                    "default": {
                        "target": "space_aware",
                        "params": {"delegate": "standard"},
                    },
                    "standard": {
                        "target": "standard",
                        "params": {"grant_store": "default", "delegation_store": "default"},
                    },
                },
                "grant_store": {"default": "memory"},
                "delegation_store": {"default": "memory"},
            }
        )
    )


@pytest.fixture
def routing_api(monkeypatch):
    """业务组件全部装上炸弹：可信性校验之前谁被碰到谁炸。"""
    kernel = _routing_kernel()
    mallory_api = kernel.api

    def _boom(*args, **kwargs):  # pragma: no cover - 断言它不该被调到
        raise AssertionError("业务组件在可信性校验之前被调用")

    monkeypatch.setattr(mallory_api._engine, "write", _boom)
    monkeypatch.setattr(mallory_api._space, "create", _boom)
    monkeypatch.setattr(mallory_api, "_invalidate_space_facts", _boom)
    return kernel, mallory_api


def _routed_add(api_obj, security):
    return api_obj.add(
        "mallory routed spam",
        Scope(org=ORG),
        security=security,
        system_metadata={"coords": {"project": "p1"}},
    )


def test_forged_routed_write_is_denied_with_zero_side_effects(routing_api) -> None:
    """P1-1 复现形态：伪造来源的 Mallory 发起路由写入，拒绝后不留任何痕迹。

    修复前：请求被拒，但 fallback 空间 ``u_mallory`` 已被创建（含归属登记、事实失效、
    审计）。修复后：SpaceManager.create、缓存失效、Engine.write 全部不被触碰。
    """
    kernel, mallory_api = routing_api
    from jiuwen_memory.common.errors import NotFoundError

    with pytest.raises(PermissionDeniedError):
        _routed_add(mallory_api, _forged(MALLORY))

    with pytest.raises(NotFoundError):
        kernel.space.get(ORG, "u_mallory")


def test_expired_routed_write_is_denied_with_zero_side_effects(routing_api) -> None:
    kernel, mallory_api = routing_api
    from jiuwen_memory.common.errors import NotFoundError

    with pytest.raises(PermissionDeniedError):
        _routed_add(mallory_api, _expired(MALLORY))

    with pytest.raises(NotFoundError):
        kernel.space.get(ORG, "u_mallory")


@pytest.fixture(scope="module", params=["forged", "expired", "tampered", "revoked"])
def invalid_security(request):
    from jiuwen_memory.config.context import AssemblyContext

    registry = CredentialStatusRegistry()
    if request.param == "forged":
        security = _forged()
    elif request.param == "expired":
        security = _expired()
    elif request.param == "tampered":
        trusted = internal_context(ScopedAuthenticator(ALICE))
        security = replace(trusted, auth=replace(trusted.auth, actor=MALLORY))
    else:
        store = KeyStoreProducer.build("memory", {}, AssemblyContext())
        registry.register("api_key", "runtime:default", store)
        security = _revoked_with(store)
    return security, registry


@pytest.mark.parametrize("entry", sorted(MemoryAPI.__abstractmethods__))
def test_every_public_entry_rejects_before_business_access(
    api, monkeypatch, invalid_security, entry
):
    """自动枚举冻结接口：新增公开方法若没提供入参或前置校验，测试会显式失败。"""
    security, registry = invalid_security
    monkeypatch.setattr(api, "_credentials", registry)

    def boom(*args, **kwargs):
        pytest.fail(f"{entry}: untrusted context reached business backend")

    # 保留装配和拒绝审计；业务端口的任何方法被触碰均失败。
    for name in (
        "_engine",
        "_queries",
        "_commands",
        "_space",
        "_scheduler",
        "_ingest_jobs",
        "_governance",
        "_space_lifecycle",
        "_membership",
        "_policy",
    ):
        component = getattr(api, name, None)
        if component is not None:
            for method in dir(type(component)):
                if not method.startswith("_") and callable(getattr(component, method, None)):
                    monkeypatch.setattr(component, method, boom)
    values = {
        "security": security,
        "scope": VICTIM_SCOPE,
        "content": "content",
        "source": Modality.TEXT,
        "items": [BatchWriteItem(content="content", scope=VICTIM_SCOPE)],
        "query": "query",
        "context": Context(scope=VICTIM_SCOPE),
        "unit_id": "unknown",
        "unit_ids": ["unknown"],
        "patch": SpacePatch() if entry == "update_space" else MemoryPatch(),
        "selector": DeleteSelector(unit_ids=["unknown"]),
        "mode": EvolveMode.EXTRACT,
        "payload_id": "payload",
        "source_ref": "source",
        "job_id": "unknown",
        "key": "key",
        "value": "value",
        "filters": {},
        "grant": Grant(
            grant_id="unknown",
            grantor=VICTIM_SCOPE,
            grantee=MALLORY,
            actions=frozenset({Action.READ}),
        ),
        "spec": SpaceSpec(org=ORG, space="new", owner=ALICE),
        "org": ORG,
        "space": VICTIM,
        "policy": SpacePolicy(),
        "member": MALLORY if entry == "remove_space_member" else SpaceMember(scope=MALLORY),
    }
    method = getattr(api, entry)
    kwargs = {}
    missing = object()
    for name, param in signature(method).parameters.items():
        if param.default is not Parameter.empty:
            continue
        value = values.get(name, missing)
        assert value is not missing, f"{entry}: missing required test argument {name!r}"
        kwargs[name] = value
    with pytest.raises(PermissionDeniedError):
        result = method(**kwargs)
        if isawaitable(result):
            asyncio.run(result)
