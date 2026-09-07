# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Access 层可用的安全能力装配辅助。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
    DevAuthenticator,
)
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.runtime import SecurityRuntime, SecurityRuntimeProducer
from jiuwen_memory.config import Config


def build_dev_authenticator() -> Authenticator:
    """构造仅供本地 HTTP / CLI 功能测试使用的固定身份认证器。"""
    return DevAuthenticator()


def build_configured_security_runtime(
    config: Mapping[str, Any] | None,
) -> SecurityRuntime | None:
    """从 ``memory_api`` 配置装配安全运行时；未声明 ``security`` 时返回 ``None``。

    Access 只传普通 mapping，不接触内核 Factory/Config。多具名实例没有 ``default``
    时仅允许唯一实例，避免生产认证模式因字典顺序被隐式选中。
    """
    register_plugins()
    parsed = Config.from_dict(config)
    ctx = parsed.context(known_top_names=Factory.known_top_names())
    names = sorted(ctx.namespaces.get(SecurityRuntimeProducer.TOP_NAME, {}))
    if not names:
        return None
    if "default" in names:
        name = "default"
    elif len(names) == 1:
        name = names[0]
    else:
        raise ValidationError(
            f"security 定义了多个具名实例 {names!r}，但未定义 'default'；"
            "安全组件选择存在歧义，拒绝启动。"
        )
    runtime = SecurityRuntimeProducer.build_named(name, ctx)
    runtime.health()
    return runtime
