"""common.authentication.base: 抽象契约与工厂注册。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from common.authentication.base import Authenticator, AuthProducer
from common.authentication.types import AuthMode, Credentials
from common.bootstrap import register_plugins
from common.credential_store.base import KeyStoreProducer, PrincipalKeyStore
from common.factory.factory import Factory

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


def test_oauth_mode_not_defined() -> None:
    """OAuth 是第二期：定义一个没有实现的枚举值只会让配置错误变成间接报错。"""
    assert {m.value for m in AuthMode} == {"dev", "trusted", "api_key"}


def test_interface_module_does_not_import_impl() -> None:
    """顶层 .py 是纯抽象，不 import *_impl/（与 control 同规）。

    检查 AST 的 import 节点，不是文本匹配——docstring 里提到实现包名是正常的。
    """
    import ast

    import common.authentication.base as auth_mod
    import common.credential_store.base as ks_mod

    for mod in (auth_mod, ks_mod):
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [name for name in imported if "_impl" in name], mod.__name__
