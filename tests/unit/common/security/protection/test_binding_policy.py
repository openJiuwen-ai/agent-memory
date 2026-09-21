"""common.security.protection.binding_policy: loopback 强制绑定策略。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.protection.binding_policy import (
    BindingPolicy,
    BindingPolicyProducer,
)
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def policy() -> BindingPolicy:
    register_plugins()
    return BindingPolicyProducer.build("loopback", {}, AssemblyContext())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"])
def test_loopback_accepted(policy, host) -> None:
    policy.check(host, requires_loopback=True)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "*", None])
def test_wildcard_rejected(policy, host) -> None:
    """容器化场景下最危险的情况：以为只是没配，实际暴露给了整个网络。"""
    with pytest.raises(ValidationError):
        policy.check(host, requires_loopback=True)


@pytest.mark.parametrize("host", ["192.168.1.10", "10.0.0.1", "example.com"])
def test_non_loopback_rejected(policy, host) -> None:
    with pytest.raises(ValidationError):
        policy.check(host, requires_loopback=True)


def test_any_dangerous_host_in_sequence_rejects(policy) -> None:
    """多网卡：任一 host 危险即拒绝，不是「有一个安全就放行」。"""
    with pytest.raises(ValidationError):
        policy.check(["127.0.0.1", "0.0.0.0"], requires_loopback=True)


def test_all_loopback_sequence_accepted(policy) -> None:
    policy.check(["127.0.0.1", "::1"], requires_loopback=True)


def test_empty_sequence_rejected(policy) -> None:
    with pytest.raises(ValidationError):
        policy.check([], requires_loopback=True)


def test_message_names_the_remedy(policy) -> None:
    """错误消息要能自解释：告诉运维改绑哪里、或改用哪个模式。"""
    with pytest.raises(ValidationError) as exc:
        policy.check("0.0.0.0", requires_loopback=True)
    message = str(exc.value)
    assert "127.0.0.1" in message
    assert "api_key" in message


def test_container_only_warns(policy, monkeypatch, caplog) -> None:
    """容器里绑 127.0.0.1 是合法的：是否暴露取决于 port mapping，框架无法检查。"""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    with caplog.at_level("WARNING"):
        policy.check("127.0.0.1", requires_loopback=True)  # 不抛
    assert any("容器" in r.message for r in caplog.records)


# -- capability 驱动而非 target 名驱动 --------------------------------------- #


@pytest.mark.parametrize("host", ["0.0.0.0", "example.com", "", None])
def test_no_loopback_requirement_allows_any_host(policy, host) -> None:
    """裁决只看认证能力自报的 ``requires_loopback_binding()``。

    声明具备远程暴露保护的实现（api_key / trusted / 第三方）可绑任意地址，
    本模块无需认识它们的 target 名。
    """
    policy.check(host, requires_loopback=False)


def test_requires_loopback_is_keyword_only(policy) -> None:
    """位置传参会让 ``check(host, False)`` 这类调用看不出放宽了什么。"""
    with pytest.raises(TypeError):
        policy.check("0.0.0.0", False)  # type: ignore[misc]


def test_check_returns_none_not_bool(policy) -> None:
    """返回 bool 会诱导调用方写 ``if not ok: log.warning(...)`` 然后照常监听。"""
    assert policy.check("127.0.0.1", requires_loopback=True) is None


def test_policy_registered_and_healthy(policy) -> None:
    register_plugins()
    assert "loopback" in BindingPolicyProducer.known()
    assert "binding_policy" in Factory.known_top_names()
    assert policy.health() is None
