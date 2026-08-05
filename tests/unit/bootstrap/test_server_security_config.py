"""Server 安全装配的 fail-closed 选择规则。"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_CORE_DIR = os.path.join("bootstrap", "core")
for _path in (_CORE_DIR, "src"):
    if _path not in sys.path:
        sys.path.append(_path)

server = importlib.import_module("server")  # noqa: E402
Config = importlib.import_module("config").Config  # noqa: E402
ValidationError = importlib.import_module("common.errors").ValidationError  # noqa: E402
register_plugins = importlib.import_module("common.bootstrap").register_plugins  # noqa: E402
Authenticator = importlib.import_module("common.authentication").Authenticator  # noqa: E402
AuthProducer = importlib.import_module("common.authentication").AuthProducer  # noqa: E402

pytestmark = pytest.mark.unit


class _CustomAuthenticator(Authenticator):
    def authenticate(self, credentials):
        raise NotImplementedError

    def mode(self) -> str:
        return "custom_remote"

    def requires_loopback_binding(self) -> bool:
        return False

    def requires_concurrency_guard(self) -> bool:
        return False

    def health(self) -> None:
        return None


def _config(data: dict) -> object:
    register_plugins()
    return Config.from_dict(data)


def test_multiple_authenticators_without_default_are_rejected() -> None:
    config = _config({"authenticator": {"local": "dev", "production": "api_key"}})

    with pytest.raises(ValidationError, match="多个具名实例"):
        server._build_authenticator(config)


def test_multiple_rate_limiters_without_default_are_rejected() -> None:
    config = _config({"rate_limiter": {"open": "unlimited", "bounded": "token_bucket"}})
    authenticator = importlib.import_module(
        "common.authentication.authentication_impl.dev_authenticator"
    ).DevAuthenticator()

    with pytest.raises(ValidationError, match="多个具名实例"):
        server._build_rate_limiter(config, authenticator)


def test_default_wins_when_multiple_security_instances_exist() -> None:
    config = _config(
        {
            "authenticator": {
                "other": "api_key",
                "default": "dev",
            }
        }
    )

    assert server._build_authenticator(config).mode().value == "dev"


def test_custom_authenticator_does_not_require_auth_mode_enum_branch() -> None:
    AuthProducer.register("custom_remote_test")(lambda _config: _CustomAuthenticator())
    try:
        config = _config({"authenticator": {"only": "custom_remote_test"}})
        authenticator = server._build_authenticator(config)

        assert authenticator.mode() == "custom_remote"
        assert server._build_argon2_guard(config, authenticator) is None
        assert (
            type(server._build_rate_limiter(config, authenticator)).__name__ == "TokenBucketLimiter"
        )
    finally:
        AuthProducer._registry.pop("custom_remote_test", None)
