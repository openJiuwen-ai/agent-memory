"""common.authentication.authentication_impl: 三个实现的正反路径与错误消息一致性。"""

from __future__ import annotations

import pytest

from common.authentication.authentication_impl.api_key_authenticator import ApiKeyAuthenticator
from common.authentication.authentication_impl.dev_authenticator import DevAuthenticator
from common.authentication.authentication_impl.trusted_authenticator import TrustedAuthenticator
from common.authentication.base import AuthProducer
from common.authentication.types import AuthMode, Credentials
from common.bootstrap import register_plugins
from common.credential_store.base import KeyStoreProducer, PrincipalKeyStore
from common.errors import AuthenticationError, ValidationError
from common.type_def.auth import AuthContext, Role
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


# -- DevAuthenticator ------------------------------------------------------- #


def test_dev_returns_root_with_empty_scope() -> None:
    """ROOT 的 actor 必须是空 Scope()。

    security.md §2.2.1 示例写 Scope(org="*")，那在本主干会被
    SQLitePermissionManager.check 的「跨 org 拒绝」规则挡住——ROOT 反而寸步难行。
    """
    ctx = DevAuthenticator().authenticate(Credentials())
    assert ctx.actor == Scope()
    assert ctx.role is Role.ROOT


def test_dev_ignores_all_credentials() -> None:
    dev = DevAuthenticator()
    assert dev.authenticate(Credentials(api_key="anything")).role is Role.ROOT
    assert dev.mode() is AuthMode.DEV
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
    assert auth.mode() is AuthMode.TRUSTED


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


def test_api_key_root_returns_empty_scope(key_store) -> None:
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key=_ROOT_KEY)
    ctx = auth.authenticate(Credentials(api_key=_ROOT_KEY))
    assert ctx.actor == Scope()
    assert ctx.role is Role.ROOT
    assert auth.mode() is AuthMode.API_KEY


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
    assert AuthProducer.build("dev", {}, ctx).mode() is AuthMode.DEV
    assert (
        AuthProducer.build("trusted", {"allow_no_gateway_key": True}, ctx).mode()
        is AuthMode.TRUSTED
    )
    assert (
        AuthProducer.build("api_key", {"root_api_key": _ROOT_KEY}, ctx).mode() is AuthMode.API_KEY
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
    assert built.mode() is AuthMode.TRUSTED
    # 配了 gateway_key 自然可装配
    assert AuthProducer.build("trusted", {"gateway_key": "k"}, ctx).mode() is AuthMode.TRUSTED
