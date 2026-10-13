"""jiuwen_memory.common.security.runtime: 能力组合、启动期健康检查与统一生命周期。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.protection.protection_impl.token_bucket_limiter import (
    TokenBucketLimiter,
)
from jiuwen_memory.common.security.protection.protection_impl.unlimited_limiter import NoRateLimit
from jiuwen_memory.common.security.runtime import SecurityRuntime, SecurityRuntimeProducer
from jiuwen_memory.config.context import AssemblyContext

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
    """五条请求路径能力缺一不可：缺一项就意味着某条边界没人把守。"""
    runtime = _build()
    assert runtime.authenticator is not None
    assert runtime.authorizer is not None
    assert runtime.rate_limiter is not None
    assert runtime.workload_guard is not None
    assert runtime.binding_policy is not None


def test_runtime_is_frozen() -> None:
    """装配后不可替换能力：运行期换掉 authenticator 等于绕过认证。"""
    runtime = _build()
    with pytest.raises(FrozenInstanceError):
        runtime.authenticator = None  # type: ignore[misc]


def test_authorizer_defaults_to_a_test_only_placeholder() -> None:
    """PR1 没有做判定的授权实现，但 ``authorizer`` 是上游固定的必填字段。

    默认装的 ``allow_all`` 占位必须自报 ``is_test_only()``——装配层据这个 capability
    在生产模式拒绝启动，而不是看 target 名（F05 §授权不变量 8）。PR1 的 PEP 仍走
    ``PermissionManager``，没有代码消费这个 Authorizer，故占位不产生放行后果。
    """
    authorizer = _build().authorizer
    assert authorizer.is_test_only() is True


def test_explicit_authorizer_overrides_the_default() -> None:
    """默认只是缺省选择，配置写出来的 target 优先——PR2 合入实现后即由配置切换。"""
    runtime = _build({"authorizer": {"target": "allow_all"}})
    assert runtime.authorizer is not None


def test_audit_integrity_is_an_optional_slot_absent_until_configured() -> None:
    """PR3 的可选装配位在 PR1 就固定：未装配即普通审计，不是 fail-open 占位。

    与必填的 ``authorizer`` 不同，消费方本就必须判空（完整性 opt-in），不存在
    「有字段就以为能用」的诱导；PR3 只填实现，不再动 Runtime 的字段形态。
    """
    assert _build().audit_integrity_provider is None


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
                        # 显式 opt-in 自动建 key file：默认 fail-closed（AUTH-ENC-01）
                        "params": {
                            "key_file": str(tmp_path / "master.key"),
                            "create_key_file": True,
                        },
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

    @staticmethod
    def health() -> None:
        raise RuntimeError("backend unreachable: token=s3cret")


def test_health_returns_none_when_all_capabilities_are_healthy() -> None:
    """返回 bool 会诱导调用方写 ``if not runtime.health(): warn(...)`` 然后照常启动。"""
    assert _build().health() is None


def test_health_rejects_when_any_capability_is_unhealthy() -> None:
    runtime = _build()
    broken = SecurityRuntime(
        authenticator=runtime.authenticator,
        authorizer=runtime.authorizer,
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
        authorizer=runtime.authorizer,
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
        cryptography_provider=_Unhealthy(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError) as exc:
        broken.health()
    assert "cryptography" in str(exc.value)
    assert "s3cret" not in str(exc.value)


def test_health_covers_authorizer() -> None:
    """授权能力同样纳入健康检查：authorizer 不健康也必须拒绝启动。"""
    runtime = _build()
    broken = SecurityRuntime(
        authenticator=runtime.authenticator,
        authorizer=_Unhealthy(),  # type: ignore[arg-type]
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
    )
    with pytest.raises(ValidationError, match="authorizer"):
        broken.health()


def test_health_covers_audit_integrity_when_configured() -> None:
    """装配了完整性 provider 就必须纳入健康检查——不能等第一条审计写失败才发现。"""
    runtime = _build()
    broken = SecurityRuntime(
        authenticator=runtime.authenticator,
        authorizer=runtime.authorizer,
        rate_limiter=runtime.rate_limiter,
        workload_guard=runtime.workload_guard,
        binding_policy=runtime.binding_policy,
        audit_integrity_provider=_Unhealthy(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError, match="audit_integrity"):
        broken.health()


def test_close_is_safe_for_capabilities_without_close() -> None:
    _build().close()


def test_close_continues_past_a_failing_capability() -> None:
    """关闭路径上放弃剩余能力会漏掉连接与文件句柄。"""
    closed: list[str] = []

    class _Failing:
        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def close() -> None:
            raise RuntimeError("close failed")

    class _Recording:
        @staticmethod
        def health() -> None:
            return None

        @staticmethod
        def close() -> None:
            closed.append("recorded")

    runtime = _build()
    combined = SecurityRuntime(
        authenticator=_Failing(),  # type: ignore[arg-type]
        authorizer=runtime.authorizer,
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
