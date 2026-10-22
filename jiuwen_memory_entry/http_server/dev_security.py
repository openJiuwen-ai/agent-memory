# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP 嵌入测试使用的完整 DEV SecurityRuntime 构造辅助。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.api import build_configured_security_runtime


def build_dev_security_runtime(*, identities: Mapping[str, Any] | None = None):
    """构造包含 BindingPolicy 等保护能力的完整 DEV SecurityRuntime。"""
    runtime = build_configured_security_runtime(
        {
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {"target": "dev", "params": {"identities": identities}}
                    },
                }
            }
        }
    )
    if runtime is None:  # pragma: no cover - 上述固定配置必然产出 Runtime
        raise RuntimeError("failed to build development security runtime")
    return runtime
