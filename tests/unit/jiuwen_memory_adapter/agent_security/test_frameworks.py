"""唯一内置驱动与可插拔工厂；不完整或未知声明不能回退。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory_adapter.agent_security import (
    ConfigurationError,
    RuntimeRegistry,
    create_runtime_registry,
)

pytestmark = pytest.mark.unit


def test_incomplete_duplicate_and_mismatched_registration():
    registry = RuntimeRegistry()
    registry.register("custom", lambda: SimpleNamespace(framework="custom"))
    with pytest.raises(ConfigurationError):
        registry.resolve("custom")
    with pytest.raises(ConfigurationError):
        registry.register("custom", object)
    registry.register("wrong", lambda: create_runtime_registry().resolve("openjiuwen"))
    with pytest.raises(ConfigurationError):
        registry.resolve("wrong")


def test_registry_isolation():
    first, second = create_runtime_registry(), create_runtime_registry()
    first.register("custom", object)
    with pytest.raises(ConfigurationError):
        second.resolve("custom")
    for name in ("unknown", "langchain", "openclaw"):
        with pytest.raises(ConfigurationError):
            second.resolve(name)
