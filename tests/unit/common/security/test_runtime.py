"""common.security.runtime: 能力组合、启动期健康检查与统一生命周期。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from common.bootstrap import register_plugins
from common.errors import ValidationError
from common.factory.factory import Factory
from common.security.protection.protection_impl.token_bucket_limiter import TokenBucketLimiter
from common.security.protection.protection_impl.unlimited_limiter import NoRateLimit
from common.security.runtime import SecurityRuntime, SecurityRuntimeProducer
from config.context import AssemblyContext

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True, scope="module")
def _registered():
    register_plugins()


def _build(params: dict | None = None) -> SecurityRuntime:
    return SecurityRuntimeProducer.build(
        "standard",
        {"authenticator": {"target": "dev"}, **(params or {})},
        AssemblyContext(),
    )


# -- 装配 -------------------------------------------------------------------- #


def test_top_name_enters_config_validation() -> None:
    assert "security" in Factory.known_top_names()


def test_authenticator_is_required_without_default() -> None:
    """给认证一个默认会让「忘了配认证」静默变成某种可用配置（F05 §装配不变量 6）。"""
    with pytest.raises(ValidationError):
        SecurityRuntimeProducer.build("standard", {}, AssemblyContext())


def test_all_request_path_capabilities_are_populated() -> None:
    """四条请求路径能力缺一不可：缺一项就意味着某条边界没人把守。"""
    runtime = _build()
    assert runtime.authenticator is not None
    assert runtime.rate_limiter is not None
    assert runtime.workload_guard is not None
    assert runtime.binding_policy is not None


def test_runtime_is_frozen() -> None:
    """装配后不可替换能力：运行期换掉 authenticator 等于绕过认证。"""
    runtime = _build()
    with pytest.raises(FrozenInstanceError):
        runtime.authenticator = None  # type: ignore[misc]


def test_no_placeholder_fields_for_later_prs() -> None:
    """恒为 ``None`` 的 ``authorizer`` 会诱导消费方写 ``if runtime.authorizer:`` 的
    fail-open 分支。PR2/PR3 补齐时再加字段。"""
    assert not hasattr(_build(), "authorizer")
    assert not hasattr(_build(), "audit_integrity_provider")


# -- 默认值取保守侧 ---------------------------------------------------------- #


def test_loopback_only_deployment_defaults_to_unlimited() -> None:
    """分岔由 capability 决定，不看 target 名（F05 §依据 capability 做安全决策）。

    dev 认证声明 ``requires_loopback_binding()``：没有远端攻击面，默认限流只会卡住
    本地压测与调试脚本。
    """
    runtime = _build()
    assert runtime.authenticator.requires_loopback_binding() is True
    assert isinstance(runtime.rate_limiter, NoRateLimit)


def test_remote_capable_authenticator_defaults_to_token_bucket() -> None:
    """声明可远程暴露的认证有攻击面，默认必须是限流的那个。"""
    runtime = SecurityRuntimeProducer.build(
        "standard",
        {"authenticator": {"target": "api_key", "params": {"root_api_key": "root-key-for-tests"}}},
        AssemblyContext(),
    )
    assert runtime.authenticator.requires_loopback_binding() is False
    assert isinstance(runtime.rate_limiter, TokenBucketLimiter)


def test_explicit_rate_limiter_overrides_the_capability_default() -> None:
    runtime = _build({"rate_limiter": {"target": "token_bucket"}})
    assert isinstance(runtime.rate_limiter, TokenBucketLimiter)


def test_workload_guard_defaults_to_a_bounded_budget() -> None:
    """没配等于没读过文档：默认必须是拦住请求的那个，不是放行的那个。"""
    assert _build().workload_guard.max_concurrent >= 1


def test_binding_policy_defaults_to_loopback_enforcement() -> None:
    runtime = _build()
    runtime.binding_policy.check("127.0.0.1", requires_loopback=True)
    with pytest.raises(ValidationError):
        runtime.binding_policy.check("0.0.0.0", requires_loopback=True)


# -- 密码学是可选的，且无默认 ------------------------------------------------ #


def test_cryptography_is_absent_unless_configured() -> None:
    """默认装一个加密 provider 会凭空造出一把没人管理生命周期的根密钥。"""
    assert _build().cryptography_provider is None


def test_cryptography_is_wired_when_configured(tmp_path) -> None:
    runtime = _build(
        {
            "cryptography": {
                "target": "local",
                "params": {
                    "key_provider": {
                        "target": "local",
                        "params": {"key_file": str(tmp_path / "master.key")},
                    }
                },
            }
        }
    )
    assert runtime.cryptography_provider is not None
    runtime.health()


# -- 健康检查与生命周期 ------------------------------------------------------ #


class _Unhealthy:
    """只在 health() 上失败的探针，用来断言 Runtime 的传播行为。"""

    def health(self) -> None:
        raise RuntimeError("backend unreachable: token=s3cret")


def test_health_returns_none_when_all_capabilities_are_healthy() -> None:
    """返回 bool 会诱导调用方写 ``if not runtime.health(): warn(...)`` 然后照常启动。"""
    assert _build().health() is None


def test_health_rejects_when_any_capability_is_unhealthy() -> None:
    runtime = _build()
    broken = SecurityRuntime(
        authenticator=runtime.authenticator,
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
        cryptography_provider=_Unhealthy(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError):
        broken.health()


def test_health_error_names_the_capability_not_the_secret() -> None:
    """异常消息只带能力名——具体原因由各实现决定暴露多少（F05 §装配不变量 8）。"""
    runtime = _build()
    broken = SecurityRuntime(
        authenticator=runtime.authenticator,
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
        cryptography_provider=_Unhealthy(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError) as exc:
        broken.health()
    assert "cryptography" in str(exc.value)
    assert "s3cret" not in str(exc.value)


def test_close_is_safe_for_capabilities_without_close() -> None:
    _build().close()


def test_close_continues_past_a_failing_capability() -> None:
    """关闭路径上放弃剩余能力会漏掉连接与文件句柄。"""
    closed: list[str] = []

    class _Failing:
        def health(self) -> None:
            return None

        def close(self) -> None:
            raise RuntimeError("close failed")

    class _Recording:
        def health(self) -> None:
            return None

        def close(self) -> None:
            closed.append("recorded")

    runtime = _build()
    combined = SecurityRuntime(
        authenticator=_Failing(),  # type: ignore[arg-type]
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
        cryptography_provider=_Recording(),  # type: ignore[arg-type]
    )
    combined.close()
    assert closed == ["recorded"]


# -- 具名共享 ---------------------------------------------------------------- #


def test_named_capabilities_are_shared_across_surfaces() -> None:
    """运行期共享状态通过具名实例显式共享，不靠模块级单例（F05 §SecurityRuntime）。"""
    ctx = AssemblyContext.from_dict(
        {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": "shared_auth",
                        "workload_guard": "shared_budget",
                    },
                }
            },
            "authenticator": {"shared_auth": {"target": "dev"}},
            "workload_guard": {
                "shared_budget": {"target": "semaphore", "params": {"max_concurrent": 1}}
            },
        }
    )
    http_runtime = SecurityRuntimeProducer.build_named("default", ctx)
    mcp_runtime = SecurityRuntimeProducer.build_named("default", ctx)

    assert http_runtime is mcp_runtime
    assert http_runtime.workload_guard is mcp_runtime.workload_guard
