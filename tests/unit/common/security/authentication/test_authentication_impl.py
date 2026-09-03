"""common.security.authentication.authentication_impl: 三个实现的正反路径与错误消息一致性。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.authentication.authentication_impl.api_key_authenticator import (
    ApiKeyAuthenticator,
)
from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
    DevAuthenticator,
)
from jiuwen_memory.common.security.authentication.authentication_impl.trusted_authenticator import (
    TrustedAuthenticator,
)
from jiuwen_memory.common.security.authentication.base import AuthProducer
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    PrincipalKeyStore,
)
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit

_ROOT_KEY = "root-key-for-tests"


@pytest.fixture(scope="module")
def key_store() -> PrincipalKeyStore:
    register_plugins()
    return KeyStoreProducer.build("memory", {}, AssemblyContext())


@pytest.fixture(scope="module")
def alice_key(key_store) -> str:
    return key_store.issue(Scope(org="acme", user="alice"), Role.USER)


# -- DevAuthenticator ------------------------------------------------------- #


def test_dev_returns_root_with_named_system_actor() -> None:
    """ROOT 的 actor 是具名系统主体，权限只由 role 表达（IMPL-01 §1）。

    security.md §2.2.1 示例写 Scope(org="*")，那在本主干会被
    SQLitePermissionManager.check 的「跨 org 拒绝」规则挡住——ROOT 反而寸步难行。
    具名 system 主体 + role=ROOT 放行语义随 PR2 ``Authorizer`` 的角色闸门
    （``authorize(auth=AuthContext, ...)``）承担，PR1 权限门不消费 role。
    """
    ctx = DevAuthenticator().authenticate(Credentials())
    assert ctx.actor == Scope(org="system", user="dev")
    assert ctx.role is Role.ROOT


def test_dev_ignores_all_credentials() -> None:
    dev = DevAuthenticator()
    assert dev.authenticate(Credentials(api_key="anything")).role is Role.ROOT
    assert dev.mode() == "dev"
    assert dev.health() is None


def test_dev_actor_is_not_shared_across_authentications() -> None:
    """NEW-SEC-01：DEV ROOT 每次认证返回独立 ``Scope`` 副本。

    模块级 ``_DEV_ACTOR`` 是共享对象，且 ``Scope`` 为可变 dataclass。若认证器直接
    复用该引用，上层把 ``ctx.actor.org`` 改成受害者 org 后，后续所有 DEV 认证都带着
    被污染的 org——一次请求的改写污染整个进程的 DEV 身份。
    """
    auth = DevAuthenticator()
    first = auth.authenticate(Credentials())
    second = auth.authenticate(Credentials())
    # 两次返回不同对象（副本），而非同一共享常量
    assert first.actor is not second.actor
    # 副本字段值仍与模块级常量一致（语义不变，只断共享）
    assert first.actor == Scope(org="system", user="dev")
    # 任意一方被改写都不影响另一方，也不影响下一次认证
    first.actor.org = "attacker"
    third = auth.authenticate(Credentials())
    assert second.actor.org == "system"
    assert third.actor == Scope(org="system", user="dev")


# -- TrustedAuthenticator --------------------------------------------------- #


def _gateway_headers(**overrides) -> dict[str, str]:
    headers = {
        "x-org-id": "acme",
        "x-principal-type": "user",
        "x-principal-id": "alice",
    }
    headers.update(overrides)
    return headers


def test_trusted_accepts_registered_principal(key_store, alice_key) -> None:
    auth = TrustedAuthenticator(key_store=key_store)
    ctx = auth.authenticate(Credentials(headers=_gateway_headers()))
    assert ctx.actor == Scope(org="acme", user="alice")
    assert ctx.role is Role.USER
    assert auth.mode() == "trusted"


def test_trusted_ignores_role_header(key_store, alice_key) -> None:
    """§2.2.2 关键设计：header 说「你是谁」，框架自己查「你能干什么」。

    这条防的是网关被攻破或误配时的任意提权。
    """
    auth = TrustedAuthenticator(key_store=key_store)
    ctx = auth.authenticate(
        Credentials(headers=_gateway_headers(**{"x-role": "root", "x-principal-role": "root"}))
    )
    assert ctx.role is Role.USER


def test_trusted_rejects_unregistered_principal(key_store) -> None:
    """未注册主体一律拒绝，不默认给 USER 放行。"""
    auth = TrustedAuthenticator(key_store=key_store)
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(headers=_gateway_headers(**{"x-principal-id": "nobody"})))


@pytest.mark.parametrize(
    "overrides",
    [
        {"x-principal-type": "admin"},  # 非 user/agent
        {"x-principal-type": ""},
        {"x-org-id": ""},
        {"x-principal-id": ""},
        {"x-org-id": "   "},  # 只有空白
    ],
)
def test_trusted_rejects_malformed_headers(key_store, overrides) -> None:
    auth = TrustedAuthenticator(key_store=key_store)
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(headers=_gateway_headers(**overrides)))


def test_trusted_requires_gateway_key_when_configured(key_store, alice_key) -> None:
    auth = TrustedAuthenticator(key_store=key_store, gateway_key="shared-secret")
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(headers=_gateway_headers()))  # 没带
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key="wrong", headers=_gateway_headers()))
    ctx = auth.authenticate(Credentials(api_key="shared-secret", headers=_gateway_headers()))
    assert ctx.actor == Scope(org="acme", user="alice")


def test_trusted_gateway_key_survives_non_ascii(key_store, alice_key) -> None:
    """compare_digest 的 str 版对非 ASCII 抛 TypeError → 500 而非 401。"""
    auth = TrustedAuthenticator(key_store=key_store, gateway_key="shared-secret")
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key="密钥", headers=_gateway_headers()))


# -- ApiKeyAuthenticator ---------------------------------------------------- #


def test_api_key_root_returns_named_system_actor(key_store) -> None:
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY, name="primary")
    ctx = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert ctx.actor == Scope(org="system", user="root")
    assert ctx.role is Role.ROOT
    assert ctx.credential_issuer == "primary"
    assert ctx.credential_status_required is False
    assert auth.mode() == "api_key"


def test_api_key_root_actor_is_not_shared_across_authentications(key_store) -> None:
    """NEW-SEC-01：Root API Key 的 ROOT 主体每次认证返回独立 ``Scope`` 副本。

    ``_ROOT_ACTOR`` 是模块级共享对象，且 ``Scope`` 为可变 dataclass。若直接复用
    该引用，上层把 ``ctx.actor.org`` 改成受害者 org 后，后续所有 Root Key 认证都带
    着被污染的 org。主体注册表分支（``replace(identity, ...)`` 已是副本）不受影响，
    本条专钉 Root Key 分支——它与 DEV 是 NEW-SEC-01 的两个污染来源。
    """
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY, name="primary")
    first = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    second = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert first.actor is not second.actor
    assert first.actor == Scope(org="system", user="root")
    first.actor.org = "attacker"
    third = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert second.actor.org == "system"
    assert third.actor == Scope(org="system", user="root")


def test_api_key_resolves_principal(key_store, alice_key) -> None:
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY, name="primary")
    ctx = auth.authenticate(Credentials(api_key=alice_key))
    assert ctx.actor == Scope(org="acme", user="alice")
    assert ctx.role is Role.USER
    assert ctx.auth_method == "api_key"
    assert ctx.credential_issuer == "primary"
    assert ctx.credential_status_required is True


def test_inline_api_key_authenticator_can_bind_runtime_issuer(key_store, alice_key) -> None:
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    auth.bind_instance_name("runtime:edge")
    ctx = auth.authenticate(Credentials(api_key=alice_key))
    assert ctx.credential_issuer == "runtime:edge"


@pytest.mark.parametrize("bad", ["", "wrong-key", "密钥非ascii", "a" * 500])
def test_api_key_rejects_bad_keys(key_store, bad) -> None:
    """非 ASCII 必须走 AuthenticationError（401），不能是 TypeError（500）。"""
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=bad))


def test_api_key_works_without_root_key(key_store, alice_key) -> None:
    """root key 已轮换掉、只留主体 key 的部署是合法的。"""
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="")
    assert auth.authenticate(Credentials(api_key=alice_key)).role is Role.USER
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=_ROOT_KEY))


class _StoreWithoutRevocation(PrincipalKeyStore):
    """可插拔 Store 漏实现 is_revoked 的最小桩（继承默认 NotImplementedError）。"""

    def issue(self, actor: Scope, role: Role) -> str:
        raise NotImplementedError

    def resolve(self, api_key: str) -> AuthContext | None:
        return None

    def revoke(self, key_fp: str) -> None:
        return None

    def get_role(self, actor: Scope) -> Role | None:
        return Role.USER

    def health(self) -> None:
        return None


def test_api_key_rejects_store_without_revocation_query() -> None:
    """可插拔 KeyStore 缺 is_revoked 时，认证期就拒绝（P1-3）。

    不让 PEP 在首个授权请求才发现 NotImplementedError（500）--F05 §装配不变量
    「不健康能力启动期拒绝」在认证边界的落地。
    """
    auth = ApiKeyAuthenticator(_StoreWithoutRevocation())
    with pytest.raises(ValidationError):
        auth.authenticate(Credentials(api_key="any-key"))


def test_api_key_store_is_revoked_drives_revocation(key_store) -> None:
    """InMemoryKeyStore 覆盖 is_revoked：撤销前 False、撤销后 True（P1-3 在线复核基础）。"""
    fresh = key_store.issue(Scope(org="acme", user="carol"), Role.USER)
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    ctx = auth.authenticate(Credentials(api_key=fresh))
    assert key_store.is_revoked(ctx.credential_id) is False
    key_store.revoke(ctx.credential_id)
    assert key_store.is_revoked(ctx.credential_id) is True


def test_trusted_credential_id_changes_with_gateway_key(key_store) -> None:
    """网关凭据轮换后，同主体得到不同 credential_id（P2-1）。

    credential_id 含 gateway_key 指纹：旧凭据绑定的委托不能迁移到新凭据。
    """
    headers = _gateway_headers()
    before = TrustedAuthenticator(key_store=key_store, gateway_key="gw-v1").authenticate(
        Credentials(api_key="gw-v1", headers=headers)
    )
    after = TrustedAuthenticator(key_store=key_store, gateway_key="gw-v2").authenticate(
        Credentials(api_key="gw-v2", headers=headers)
    )
    assert before.credential_id
    assert before.credential_id != after.credential_id


# -- 跨实现的一致性 ---------------------------------------------------------- #


def test_all_failures_share_one_message(key_store) -> None:
    """错误消息若区分「主体不存在」与「凭据错误」，就成了主体枚举侧信道。

    这条是防止后续维护者「好心」加详细错误消息的护栏。
    """
    api_key_auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    trusted_auth = TrustedAuthenticator(key_store=key_store, gateway_key="s")

    messages = set()
    for auth, creds in (
        (api_key_auth, Credentials()),  # 凭据缺失
        (api_key_auth, Credentials(api_key="wrong")),  # 凭据错误
        (trusted_auth, Credentials(headers={})),  # 声明缺失
        (trusted_auth, Credentials(headers=_gateway_headers())),  # 网关密钥缺失
        (
            trusted_auth,
            Credentials(api_key="s", headers=_gateway_headers(**{"x-principal-id": "ghost"})),
        ),  # 主体不存在
    ):
        with pytest.raises(AuthenticationError) as exc:
            auth.authenticate(creds)
        messages.add(str(exc.value))

    assert messages == {"authentication failed"}


def test_authenticate_never_returns_none(key_store, alice_key) -> None:
    """认证只有成功与失败两种结果——返回 None 会诱导 fail-open 分支。"""
    for auth, creds in (
        (DevAuthenticator(), Credentials()),
        (TrustedAuthenticator(key_store=key_store), Credentials(headers=_gateway_headers())),
        (
            ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY),
            Credentials(api_key=alice_key),
        ),
    ):
        assert isinstance(auth.authenticate(creds), AuthContext)


def test_producer_builds_each_mode() -> None:
    register_plugins()
    ctx = AssemblyContext()
    assert AuthProducer.build("dev", {}, ctx).mode() == "dev"
    assert AuthProducer.build("trusted", {"allow_no_gateway_key": True}, ctx).mode() == "trusted"
    assert AuthProducer.build("api_key", {"root_api_key": _ROOT_KEY}, ctx).mode() == "api_key"


def test_trusted_build_requires_gateway_key_by_default() -> None:
    """审计 P1-2：未配 gateway_key 时默认拒绝装配，fail-closed。

    未配置时全部身份 header 可被任意调用方伪造；让它默认启动等于把信任边界
    留给「配没配网关」这个隐含假设。显式 opt-in 才放行。
    """
    register_plugins()
    ctx = AssemblyContext()
    with pytest.raises(ValidationError):
        AuthProducer.build("trusted", {}, ctx)
    # 显式 opt-in 后可装配
    built = AuthProducer.build("trusted", {"allow_no_gateway_key": True}, ctx)
    assert built.mode() == "trusted"
    # 配了 gateway_key 自然可装配
    assert AuthProducer.build("trusted", {"gateway_key": "k"}, ctx).mode() == "trusted"


def test_root_key_secret_value_flows_through_assembly(key_store) -> None:
    """
    AUTH-ENC-03：root_api_key 经 from_dict 包成 SecretValue 后，装配边界仍能
    取到明文--脱敏只影响打印，不影响认证。
    """
    register_plugins()
    ctx = AssemblyContext.from_dict(
        {
            "authenticator": {
                "primary": {
                    "target": "api_key",
                    "params": {"root_api_key": _ROOT_KEY},
                }
            }
        },
        known_top_names={"authenticator"},
    )
    assert repr(ctx.lookup("authenticator", "primary").params["root_api_key"]).startswith(
        "<SecretValue sha256:"
    )
    auth = AuthProducer.build_named("primary", ctx)
    assert auth.mode() == "api_key"
    ctx_obj = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert ctx_obj.actor.user == "root"
