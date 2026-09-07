# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式本地 DEV 模式的配置适配。

DEV 是 composition root 的显式选择，不是安全能力内部的运行期分支。本模块只在用户没有
声明 ``security`` 时补齐一个完整的 DEV SecurityRuntime 配置，并在 PR2 接管 ROOT 角色前
补上既有 ``allow_all`` PermissionManager 兼容项；显式配置始终优先。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any


def with_local_dev_security(config: Any) -> Any:
    """返回注入完整 DEV 安全配置的副本，不修改调用方的 Config。"""
    settings = dict(config.settings)
    memory_api = dict(settings.get("memory_api") or {})
    if "security" in memory_api:
        return config

    memory_api["security"] = {
        "default": {
            "target": "standard",
            "params": {"authenticator": {"target": "dev"}},
        }
    }
    memory_api.setdefault("permission", {"default": {"target": "allow_all"}})
    settings["memory_api"] = memory_api
    return replace(config, settings=settings)
