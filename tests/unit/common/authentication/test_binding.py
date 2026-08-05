"""common.authentication.binding: DEV 模式的 localhost 强制绑定。"""

from __future__ import annotations

import pytest

from common.authentication.binding import check_dev_binding
from common.errors import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"])
def test_loopback_accepted(host) -> None:
    check_dev_binding(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "*", None])
def test_wildcard_rejected(host) -> None:
    """容器化场景下最危险的情况：以为只是没配，实际暴露给了整个网络。"""
    with pytest.raises(ValidationError):
        check_dev_binding(host)


@pytest.mark.parametrize("host", ["192.168.1.10", "10.0.0.1", "example.com"])
def test_non_loopback_rejected(host) -> None:
    with pytest.raises(ValidationError):
        check_dev_binding(host)


def test_any_dangerous_host_in_sequence_rejects() -> None:
    """多网卡：任一 host 危险即拒绝，不是「有一个安全就放行」。"""
    with pytest.raises(ValidationError):
        check_dev_binding(["127.0.0.1", "0.0.0.0"])


def test_all_loopback_sequence_accepted() -> None:
    check_dev_binding(["127.0.0.1", "::1"])


def test_empty_sequence_rejected() -> None:
    with pytest.raises(ValidationError):
        check_dev_binding([])


def test_message_names_the_remedy() -> None:
    """错误消息要能自解释：告诉运维改绑哪里、或改用哪个模式。"""
    with pytest.raises(ValidationError) as exc:
        check_dev_binding("0.0.0.0")
    message = str(exc.value)
    assert "127.0.0.1" in message
    assert "api_key" in message


def test_container_only_warns(monkeypatch, caplog) -> None:
    """容器里绑 127.0.0.1 是合法的：是否暴露取决于 port mapping，框架无法检查。"""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    with caplog.at_level("WARNING"):
        check_dev_binding("127.0.0.1")  # 不抛
    assert any("容器" in r.message for r in caplog.records)
