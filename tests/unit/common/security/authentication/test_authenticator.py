"""jiuwen_memory.common.security.authentication.base / key_store: 抽象契约与工厂注册。

接口先行版当前只合入本地测试用 ``dev`` 实现；工厂断言覆盖 Producer 契约、
TOP_NAME 配置校验、注册幂等和 dev 注册项。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.authentication.key_store import (
    PrincipalKeyStore,
    fingerprint,
    generate_api_key,
    key_prefix,
)
from jiuwen_memory.common.security.types import Credentials

pytestmark = pytest.mark.unit


def test_registration_is_idempotent() -> None:
    register_plugins()
    first = AuthProducer.known()
    register_plugins()
    assert AuthProducer.known() == first
    assert "dev" in first


def test_top_names_enter_config_validation() -> None:
    """顶层段名要进 Factory.known_top_names()，否则配置解析期会拒掉这两段。"""
    register_plugins()
    tops = Factory.known_top_names()
    assert "authenticator" in tops
    assert "key_store" in tops


def test_abstract_contract_cannot_be_partially_implemented() -> None:
    class Incomplete(Authenticator):
        def authenticate(self, credentials: Credentials):  # 缺 mode / health
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_capability_declarations_default_to_fail_closed() -> None:
    """未覆写的 capability 取保守侧：第三方实现不声明就不享受放宽。

    ``requires_loopback_binding`` 默认 True--没声明具备远程暴露保护的实现不许
    绑非本机地址；``requires_concurrency_guard`` 默认 True--没声明成本模型的
    校验器不许绕过并发预算。两处默认反过来都是 fail-open。
    """

    class Minimal(Authenticator):
        def authenticate(self, credentials: Credentials):
            raise NotImplementedError

        def mode(self) -> str:
            return "minimal"

        def health(self) -> None:
            return None

    minimal = Minimal()
    assert minimal.requires_loopback_binding() is True
    assert minimal.requires_concurrency_guard() is True


def test_key_store_abstract_contract() -> None:
    class Incomplete(PrincipalKeyStore):
        def issue(self, actor, role):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_credentials_is_frozen() -> None:
    creds = Credentials(api_key="k")
    with pytest.raises(FrozenInstanceError):
        creds.api_key = "other"  # type: ignore[misc]


def test_credentials_defaults_are_empty() -> None:
    creds = Credentials()
    assert creds.api_key == ""
    assert creds.headers == {}
    assert creds.peer_address == ""


def test_credentials_repr_hides_secrets() -> None:
    """凭据会进日志与异常回溯：明文 key 不能出现在 repr 里（F05 §Credentials）。"""
    assert "super-secret" not in repr(Credentials(api_key="super-secret"))


def test_fingerprint_is_sha256_hex() -> None:
    """指纹是撤销与审计的定位键：确定性、十六进制、与明文单向分离。"""
    assert fingerprint("some-key") == fingerprint("some-key")
    assert fingerprint("some-key") != fingerprint("other-key")
    assert len(fingerprint("some-key")) == 64
    int(fingerprint("some-key"), 16)  # 合法十六进制


def test_key_prefix_is_short_prefix() -> None:
    assert key_prefix("abcdef123456") == "abcdef12"
    assert key_prefix("") == ""


def test_generate_api_key_is_high_entropy_urlsafe() -> None:
    """secrets.token_urlsafe(32) -> 43 字符；连续生成不得重复。"""
    first = generate_api_key()
    second = generate_api_key()
    assert len(first) == 43
    assert first != second


def test_is_revoked_default_is_fail_closed() -> None:
    """不跟踪撤销状态的后端必须显式失败，不能静默答「未撤销」放行已撤销凭据。"""

    class NoRevocation(PrincipalKeyStore):
        def issue(self, actor, role):
            raise NotImplementedError

        def resolve(self, api_key):
            raise NotImplementedError

        def revoke(self, key_fp):
            raise NotImplementedError

        def get_role(self, actor):
            raise NotImplementedError

        def health(self) -> None:
            return None

    with pytest.raises(NotImplementedError):
        NoRevocation().is_revoked("deadbeef")


def test_interface_module_does_not_import_impl() -> None:
    """顶层 .py 是纯抽象，不 import *_impl/（与 control 同规）。

    检查 AST 的 import 节点，不是文本匹配--docstring 里提到实现包名是正常的。
    """
    import ast

    import jiuwen_memory.common.security.authentication.base as auth_mod
    import jiuwen_memory.common.security.authentication.credential_registry as reg_mod
    import jiuwen_memory.common.security.authentication.key_store as ks_mod

    for mod in (auth_mod, ks_mod, reg_mod):
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [name for name in imported if "_impl" in name], mod.__name__
