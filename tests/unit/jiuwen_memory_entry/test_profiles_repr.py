"""profiles.Config：secret 参数的 repr 脱敏（AUTH-ENC-03，F05 装配不变量 7）。"""

# ruff: noqa: E402

from __future__ import annotations

import os
import sys

import pytest

from jiuwen_memory.api import ValidationError

_CORE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "jiuwen_memory_entry", "core")
)
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

import profiles


def test_config_repr_redacts_secrets() -> None:
    layer = {
        "profile": "offline",
        "memory_api": {
            "authenticator": {
                "primary": {
                    "target": "api_key",
                    "params": {"root_api_key": "sk-plaintext-root-123"},
                }
            }
        },
    }
    cfg = profiles.load_config([layer])
    assert "sk-plaintext-root-123" not in repr(cfg)
    assert "<redacted>" in repr(cfg)
    # 明文仍可从 settings 取到：脱敏只影响打印，不影响装配。
    assert cfg.settings["memory_api"]["authenticator"]["primary"]["params"]["root_api_key"] == (
        "sk-plaintext-root-123"
    )


def test_empty_policies_section_is_an_empty_map() -> None:
    """``policies:``（空 YAML 段）与缺省同义，装配期不得抛 TypeError。"""
    cfg = profiles.load_config([{"profile": "offline", "policies": None}])

    assert cfg.policies == {}


def test_non_object_policies_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="policies must be an object"):
        profiles.load_config([{"policies": ["scope.require_space"]}])
