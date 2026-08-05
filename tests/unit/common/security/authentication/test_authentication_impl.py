"""common.security.authentication.authentication_impl: 三个实现的正反路径与错误消息一致性。"""

from __future__ import annotations

import pytest

from common.bootstrap import register_plugins
from common.errors import AuthenticationError, ValidationError
from common.security.authentication.authentication_impl.api_key_authenticator import (
    ApiKeyAuthenticator,
)
from common.security.authentication.authentication_impl.dev_authenticator import (
    DevAuthenticator,
)
from common.security.authentication.authentication_impl.trusted_authenticator import (
    TrustedAuthenticator,
)
from common.security.authentication.base import AuthProducer
from common.security.authentication.key_store import KeyStoreProducer, PrincipalKeyStore
from common.security.types import AuthContext, Credentials, Role
from common.type_def.scope import Scope
from config.context import AssemblyContext

pytestmark = pytest.mark.unit

_ROOT_KEY = "root-key-for-tests"


@pytest.fixture(scope="module")
def key_store() -> PrincipalKeyStore:
    register_plugins()
    return KeyStoreProducer.build("memory", {}, AssemblyContext())


@pytest.fixture(scope="module")
def alice_key(key_store) -> str:
    return key_store.issue(Scope(org="acme", user="alice"), Role.USER)


@pytest.fixture(scope="module")
def agent_key(key_store) -> str:
    return key_store.issue(Scope(org="acme", agent="assistant"), Role.USER)


# -- DevAuthenticator ------------------------------------------------------- #


def test_dev_root_is_a_named_principal_not_an_empty_scope() -> None:
    """ROOT 由 ``role`` 表达，不由 actor 的形状表达（F05 §授权不变量 1）。

    旧行为是「空 ``Scope()`` 即管理员」：授权侧只要漏判一次 actor 就等于放行，且审计
    里所有 ROOT 动作都记成同一个无名主体、追不到人。现在 dev 是具名的
    ``system/dev``，权限完全来自 ``role is Role.ROOT``。
    """
    ctx = DevAuthenticator().authenticate(Credentials())
    assert ctx.actor == Scope(org="system", user="dev")
    assert ctx.role is Role.ROOT


def test_dev_ignores_all_credentials() -> None:
    dev = DevAuthenticator()
    assert dev.authenticate(Credentials(api_key="anything")).role is Role.ROOT
    assert dev.mode() == "dev"
    assert dev.health() is None


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


def test_trusted_does_not_accept_acting_user_header(key_store, agent_key) -> None:
    """``X-Acting-User`` 不再产生任何跨主体授权（F05 §从 header 直接产生 Delegation）。

    这个 header 曾直接写进 ``AuthContext.acting_user``，旧 PermissionManager 据此放行
    agent 代 user 的读写。网关的一句声明就成了跨主体授权结论，中间没有服务端事实。
    """
    auth = TrustedAuthenticator(key_store=key_store)
    headers = _gateway_headers(
        **{
            "x-principal-type": "agent",
            "x-principal-id": "assistant",
            "x-acting-user": "alice",
        }
    )
    ctx = auth.authenticate(Credentials(headers=headers))
    assert ctx.actor == Scope(org="acme", agent="assistant")
    assert not hasattr(ctx, "acting_user")
    assert ctx.delegation_id == ""


def test_trusted_carries_delegation_id_without_validating_it(key_store, agent_key) -> None:
    """``X-Delegation-Id`` 只是原样带过来的**标识**，认证层不作任何有效性判断。

    有效性（存在、未撤销、未过期、覆盖本次动作）由 Authorizer 回 DelegationStore 复核。
    认证层这里放行一个查无此据的 id 是对的——伪造 id 的拒绝发生在授权层，且与
    「已撤销」「已过期」共用同一个 reason，不构成委托枚举侧信道。
    """
    auth = TrustedAuthenticator(key_store=key_store)
    headers = _gateway_headers(
        **{
            "x-principal-type": "agent",
            "x-principal-id": "assistant",
            "x-delegation-id": "  d-42  ",
        }
    )
    ctx = auth.authenticate(Credentials(headers=headers))
    assert ctx.delegation_id == "d-42"


# -- ApiKeyAuthenticator ---------------------------------------------------- #


def test_api_key_root_is_a_named_principal(key_store) -> None:
    """root key 换到的同样是具名主体 + ROOT 角色，不是空 Scope 管理员。

    ``StandardAuthorizer`` 第 2 步对 ``actor == Scope()`` 直接 deny：空 actor 现在
    是「上下文不完整」的信号，不再是任何一种权限。
    """
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    ctx = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert ctx.actor == Scope(org="system", user="root")
    assert ctx.role is Role.ROOT
    assert auth.mode() == "api_key"


def test_api_key_resolves_principal(key_store, alice_key) -> None:
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    ctx = auth.authenticate(Credentials(api_key=alice_key))
    assert ctx.actor == Scope(org="acme", user="alice")
    assert ctx.role is Role.USER


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
    assert (
        AuthProducer.build("trusted", {"allow_no_gateway_key": True}, ctx).mode()
        == "trusted"
    )
    assert (
        AuthProducer.build("api_key", {"root_api_key": _ROOT_KEY}, ctx).mode() == "api_key"
    )


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
