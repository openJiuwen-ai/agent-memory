# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开 Server 装配句柄的选择、失败清理和生命周期契约。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    MemoryRuntime,
    ValidationError,
    assemble_server_runtime,
    build_configured_security_runtime,
)
from jiuwen_memory.api.memory_api_impl import assembly

pytestmark = pytest.mark.unit

_DEV_CONFIG = {
    "security": {"default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}}
}


def test_server_runtime_none_disables_selection_not_kernel_authorization() -> None:
    runtime = assemble_server_runtime(config=_DEV_CONFIG, security_runtime=None)
    try:
        assert runtime.security_runtime is None
        assert runtime.api is not None
        assert runtime.audit is not None
    finally:
        runtime.close()


def test_server_runtime_auto_uses_configured_runtime() -> None:
    runtime = assemble_server_runtime(config=_DEV_CONFIG)
    selected = runtime.security_runtime
    assert selected is not None
    try:
        selected.health()
        independent = build_configured_security_runtime(_DEV_CONFIG)
        try:
            assert selected is not independent
        finally:
            independent.close()
        selected.health()
    finally:
        runtime.close()
    assert selected.security_resource_owner().is_closed()


@pytest.mark.parametrize("error_type", [ValidationError, KeyboardInterrupt])
def test_failed_server_binding_closes_kernel_not_injected_runtime(monkeypatch, error_type) -> None:
    created = []
    events = []
    convert = assembly._runtime_from_kernel  # pylint: disable=protected-access

    def capture_runtime(kernel):
        runtime = convert(kernel)
        created.append(runtime)
        return runtime

    def fail_health():
        raise error_type("injected health failure")

    injected = SimpleNamespace(health=fail_health, close=lambda: events.append("injected"))
    monkeypatch.setattr(assembly, "_runtime_from_kernel", capture_runtime)
    with pytest.raises(error_type, match="injected health failure"):
        assemble_server_runtime(config={}, security_runtime=injected)
    assert len(created) == 1
    assert created[0].security_resource_owner().is_closed()
    assert events == [], "failed binding must not adopt the injected runtime"


def test_server_runtime_rejects_closed_explicit_security() -> None:
    original = assemble_server_runtime(config=_DEV_CONFIG)
    selected = original.security_runtime
    original.close()
    with pytest.raises(ValidationError, match="already been closed"):
        assemble_server_runtime(config={}, security_runtime=selected)


def test_server_api_close_delegates_to_complete_facade() -> None:
    events = []
    selected = SimpleNamespace(close=lambda: events.append("selected"))
    runtime = assemble_server_runtime(config={}, security_runtime=selected)
    runtime.api.close()
    assert events == ["selected"], "API shutdown must not bypass the selected security runtime"


def test_memory_runtime_public_contract_includes_sdk_authentication() -> None:
    public = {name for name in vars(MemoryRuntime) if not name.startswith("_")}
    assert public == {"api", "authenticate", "start", "start_background", "close"}
