"""显式运行时驱动注册；框架与记忆后端独立扩展。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ._validation import has_exact_type
from .config import AgentSecurityConfig, ConfigurationError
from .runtime import Actor

if TYPE_CHECKING:
    from .execution import ExecutionAdapter
    from .llm import LlmAdapter
    from .results import ResultGuard


@dataclass(frozen=True)
class AgentRunContext:
    actor: Actor
    turn: str
    recipient: Actor | None = None
    operation_id: str = ""


@dataclass(frozen=True)
class RuntimeContext:
    """原生驱动的窄装配端口；不给驱动整个 SecurityPlugin 或可写收据注册表。"""

    llm: LlmAdapter
    execution: ExecutionAdapter
    results: ResultGuard
    current: Callable[[], AgentRunContext]
    models: Mapping[int, tuple[Any, Any]]
    tools: Mapping[int, Any]
    own_client: Callable[[Any, bool], None]


class RuntimeDriver(Protocol):
    framework: str

    def create_execution(self, guard, **ports) -> ExecutionAdapter:
        """由显式注册的实现提供此端口。"""

        ...

    def create_llm_binding(self, adapter):
        """由显式注册的实现提供此端口。"""
        ...

    def create_delivery_binding(self, adapter):
        """由显式注册的实现提供此端口。"""
        ...

    def register_llm_provider(self, adapter):
        """由显式注册的实现提供此端口。"""
        ...

    def create_tool(self, **definition):
        """由显式注册的实现提供此端口。"""
        ...

    def create_model(self, host: RuntimeContext, target, **options):
        """由显式注册的实现提供此端口。"""
        ...

    async def build_agent(self, host: RuntimeContext, **configuration):
        """由显式注册的实现提供此端口。"""
        ...

    def inputs(self, text: str, session: Actor) -> tuple[dict, dict]:
        """由显式注册的实现提供此端口。"""
        ...

    def llm_task(self, request) -> str:
        """由显式注册的实现提供此端口。"""
        ...


_METHODS = (
    "create_execution",
    "create_llm_binding",
    "create_delivery_binding",
    "register_llm_provider",
    "create_tool",
    "create_model",
    "build_agent",
    "inputs",
    "llm_task",
)


class RuntimeRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], RuntimeDriver]] = {}

    def _check_name(self, framework):
        if (
            not has_exact_type(framework, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", framework) is None
        ):
            raise ConfigurationError("运行时框架标识无效")
        if framework in self._factories:
            raise ConfigurationError("运行时框架重复注册")

    def register(self, framework: str, factory: Callable[[], RuntimeDriver]) -> None:
        self._check_name(framework)
        if not callable(factory):
            raise ConfigurationError("运行时驱动工厂不可调用")
        self._factories[framework] = factory

    def resolve(self, framework: str) -> RuntimeDriver:
        factory = self._factories.get(framework)
        if factory is None:
            raise ConfigurationError(f"未知运行时框架: {framework!r}")
        driver = factory()
        if getattr(driver, "framework", None) != framework or any(
            not callable(getattr(driver, name, None)) for name in _METHODS
        ):
            raise ConfigurationError("运行时驱动声明或能力不完整")
        return driver


def create_runtime_registry() -> RuntimeRegistry:
    registry = RuntimeRegistry()

    def openjiuwen():
        from .adapters._runtime_openjiuwen import OpenJiuwenRuntimeDriver

        return OpenJiuwenRuntimeDriver()

    registry.register("openjiuwen", openjiuwen)
    return registry


def load_runtime_driver(config, *, registry: RuntimeRegistry | None = None) -> RuntimeDriver:
    if not isinstance(config, AgentSecurityConfig):
        config = AgentSecurityConfig.from_mapping(config)
    return (registry if registry is not None else create_runtime_registry()).resolve(
        config.framework
    )
