# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Profiles & config stacking for the kernel surface (in-process assembly).

``OFFLINE`` is the no-external-deps baseline profile. ``load_config`` stacks JSON
layers nearest-wins (later layers override earlier), mirroring how the CLI passes
``--config a.json b.json`` on top of ``OFFLINE``. The minimal reference build
ignores everything but the ``profile`` name and any ``policies`` map (forwarded to
the in-memory PolicyManager); a real build would select plugins/storage here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiuwen_memory.common.security.types import SECRET_PARAM_KEYS

OFFLINE: dict[str, Any] = {"profile": "offline"}


def _redact(obj: Any) -> Any:
    """递归把 secret 键的值替换成占位符：``repr(Config)`` 不落明文（F05 装配不变量 7）。"""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in SECRET_PARAM_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


@dataclass
class Config:
    """Resolved configuration handed to :func:`server.build`."""

    profile: str = "offline"
    policies: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        # settings 是 env 展开后的完整配置：secret 键必须脱敏后才能进诊断输出。
        return (
            f"Config(profile={self.profile!r}, policies={self.policies!r}, "
            f"settings={_redact(self.settings)!r})"
        )


def load_config(layers: list[dict[str, Any]], spaces: Any = None) -> Config:
    """Merge config ``layers`` (nearest-wins) into a single :class:`Config`."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return Config(
        profile=str(merged.get("profile", "offline")),
        policies=dict(merged.get("policies", {})),
        settings=merged,
    )
