"""Server 安全装配的 fail-closed 选择规则。

装配面从三个独立的 ``_build_authenticator`` / ``_build_rate_limiter`` /
``_build_argon2_guard`` 收敛成一个 ``build_security_runtime``（返回 ``SecurityRuntime``）后，
这里测的仍是同三件事：**多实例无 default 拒绝启动**、**能力默认取保守侧**、
**分岔由 capability 决定而非认证 target 名**。
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_CORE_DIR = os.path.join("jiuwen_memory_entry", "core")
for _path in (_CORE_DIR, "."):
    if _path not in sys.path:
        sys.path.append(_path)

server = importlib.import_module("server")  # noqa: E402
Config = importlib.import_module("jiuwen_memory.config").Config  # noqa: E402
ValidationError = importlib.import_module("jiuwen_memory.common.errors").ValidationError  # noqa: E402
register_plugins = importlib.import_module("jiuwen_memory.common.bootstrap").register_plugins  # noqa: E402
Factory = importlib.import_module("jiuwen_memory.common.factory.factory").Factory  # noqa: E402
_auth = importlib.import_module("jiuwen_memory.common.security.authentication")  # noqa: E402
Authenticator = _auth.Authenticator  # noqa: E402
AuthProducer = _auth.AuthProducer  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_instances():
    """每个用例独立装配：具名实例缓存会让同名 ``security.only`` 跨用例复用。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


class _CustomAuthenticator(Authenticator):
    """第三方认证实现：核心不认识它的 target 名，只读它自报的 capability。"""

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


# -- 歧义配置拒绝启动（F05 §装配不变量 3）------------------------------------ #


def test_multiple_security_instances_without_default_are_rejected() -> None:
    config = _config(
        {
            "security": {
                "local": {"target": "standard", "params": {"authenticator": {"target": "dev"}}},
                "production": {
                    "target": "standard",
                    "params": {"authenticator": {"target": "dev"}},
                },
            }
        }
    )

    with pytest.raises(ValidationError, match="多个具名实例"):
        server.build_security_runtime(config)


def test_default_wins_when_multiple_security_instances_exist() -> None:
    config = _config(
        {
            "security": {
                "other": {
                    "target": "standard",
                    "params": {
                        "authenticator": {
                            "target": "api_key",
                            "params": {"root_api_key": "root-key-for-tests"},
                        }
                    },
                },
                "default": {
                    "target": "standard",
                    "params": {"authenticator": {"target": "dev"}},
                },
            }
        }
    )

    assert server.build_security_runtime(config).authenticator.mode() == "dev"


def test_single_unnamed_instance_is_used_without_default() -> None:
    """只有一个实例时不存在歧义，不强求叫 ``default``。"""
    config = _config(
        {
            "security": {"only": {"target": "standard", "params": {"authenticator": "dev_auth"}}},
            "authenticator": {"dev_auth": {"target": "dev"}},
        }
    )

    assert server.build_security_runtime(config).authenticator.mode() == "dev"


# -- 无 security 段回落 DEV（显式、可切换，非隐式不可改）--------------------- #


def test_missing_security_section_falls_back_to_dev_with_a_warning(caplog) -> None:
    """回落不是静默的：日志必须说明「现在没有认证」以及怎么改。"""
    config = _config({})

    with caplog.at_level("WARNING"):
        runtime = server.build_security_runtime(config)

    assert runtime.authenticator.mode() == "dev"
    assert any("security" in r.message for r in caplog.records)


def test_dev_fallback_still_enforces_loopback_binding() -> None:
    """回落 DEV 不等于放开绑定：非 loopback 由 binding_policy 在绑定前拒绝。"""
    runtime = server.build_security_runtime(_config({}))

    runtime.binding_policy.check(
        "127.0.0.1", requires_loopback=runtime.authenticator.requires_loopback_binding()
    )
    with pytest.raises(ValidationError):
        runtime.binding_policy.check(
            "0.0.0.0", requires_loopback=runtime.authenticator.requires_loopback_binding()
        )


# -- 分岔由 capability 决定，不看 target 名（F05 §依据 capability 做安全决策）- #


def test_custom_authenticator_does_not_require_a_target_name_branch() -> None:
    """核心不认识 ``custom_remote_test``，仍能按它自报的 capability 装配出正确默认。"""
    AuthProducer.register("custom_remote_test")(lambda _config: _CustomAuthenticator())
    try:
        config = _config(
            {
                "security": {
                    "only": {
                        "target": "standard",
                        "params": {"authenticator": {"target": "custom_remote_test"}},
                    }
                }
            }
        )
        runtime = server.build_security_runtime(config)

        assert runtime.authenticator.mode() == "custom_remote"
        # 声明可远程暴露 -> 默认限流；声明不需要预算 -> Server 不把预算传给中间件。
        assert type(runtime.rate_limiter).__name__ == "TokenBucketLimiter"
        assert server.Server(config, None, runtime).workload_guard is None
    finally:
        # Producer 注册表没有运行期卸载语义；测试只需恢复本次临时注册。
        vars(AuthProducer)["_registry"].pop("custom_remote_test", None)


def test_loopback_only_authenticator_defaults_to_no_rate_limit() -> None:
    """dev 声明 requires_loopback_binding：无远端攻击面，默认限流只会卡住本地调试。"""
    runtime = server.build_security_runtime(_config({}))

    assert runtime.authenticator.requires_loopback_binding() is True
    assert type(runtime.rate_limiter).__name__ == "NoRateLimit"


def test_workload_guard_is_withheld_when_the_authenticator_declares_no_need() -> None:
    """预算是否传给中间件由认证实现的成本模型决定，不由 Server 猜。

    api_key 每次 authenticate 跑一次 Argon2id verify，未声明豁免（基类默认 True）
    -> 拿得到预算；dev 显式声明无重型校验 -> 拿不到。
    """
    api_key_config = _config(
        {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {
                            "target": "api_key",
                            "params": {"root_api_key": "root-key-for-tests"},
                        }
                    },
                }
            }
        }
    )
    expensive = server.build_security_runtime(api_key_config)
    assert expensive.authenticator.requires_concurrency_guard() is True
    assert server.Server(api_key_config, None, expensive).workload_guard is not None

    dev_config = _config({})
    cheap = server.build_security_runtime(dev_config)
    assert cheap.authenticator.requires_concurrency_guard() is False
    assert server.Server(dev_config, None, cheap).workload_guard is None
