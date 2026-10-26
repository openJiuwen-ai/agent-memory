# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式本地 DEV 模式的配置适配。

DEV 是 composition root 的显式选择，不是安全能力内部的运行期分支。本模块只在用户没有
声明 ``security`` 时补齐默认 USER 的 DEV SecurityRuntime 配置；显式配置始终优先。
ROOT 须在认证器配置中显式选择；权限始终由 Authorizer 判定，不注入 allow_all 过渡项。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any


def with_local_dev_security(config: Any, *, identities: Mapping[str, Any] | None = None) -> Any:
    """返回注入完整 DEV 安全配置的副本，不修改调用方的 Config。"""
    settings = dict(config.settings)
    memory_api = dict(settings.get("memory_api") or {})
    if "security" in memory_api:
        return config

    memory_api["security"] = {
        "default": {
            "target": "standard",
            "params": {"authenticator": {"target": "dev", "params": {"identities": identities}}},
        }
    }
    settings["memory_api"] = memory_api
    return replace(config, settings=settings)
