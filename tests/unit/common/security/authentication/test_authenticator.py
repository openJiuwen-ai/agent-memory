"""common.security.authentication.base / key_store: 抽象契约与工厂注册。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from common.bootstrap import register_plugins
from common.factory.factory import Factory
from common.security.authentication.base import Authenticator, AuthProducer
from common.security.authentication.key_store import KeyStoreProducer, PrincipalKeyStore
from common.security.types import Credentials
from config.context import AssemblyContext

pytestmark = pytest.mark.unit


def test_registration_is_idempotent() -> None:
    register_plugins()
    first = AuthProducer.known()
    register_plugins()
    assert AuthProducer.known() == first


def test_all_three_modes_registered() -> None:
    register_plugins()
    assert AuthProducer.known() == ["api_key", "dev", "trusted"]
    assert KeyStoreProducer.known() == ["memory"]


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

    ``requires_loopback_binding`` 默认 True——没声明具备远程暴露保护的实现不许
    绑非本机地址；``requires_concurrency_guard`` 默认 True——没声明成本模型的
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


def test_mode_is_an_open_string_not_a_closed_enum() -> None:
    """F05 拒绝以封闭枚举驱动核心分支：第三方实现不改核心即可声明自己的模式名。"""
    register_plugins()
    mode = AuthProducer.build("dev", {}, AssemblyContext()).mode()
    assert isinstance(mode, str)
    assert mode == "dev"


def test_interface_module_does_not_import_impl() -> None:
    """顶层 .py 是纯抽象，不 import *_impl/（与 control 同规）。

    检查 AST 的 import 节点，不是文本匹配——docstring 里提到实现包名是正常的。
    """
    import ast

    import common.security.authentication.base as auth_mod
    import common.security.authentication.key_store as ks_mod

    for mod in (auth_mod, ks_mod):
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [name for name in imported if "_impl" in name], mod.__name__
