"""部署模板不得用实例级默认值遮蔽 QueryParser 的 globals 开关。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_SWITCHES = {"sanitize_enabled", "sanitize_strip_code", "rewrite_enabled"}


@pytest.mark.parametrize("profile", ("local", "online", "postgres"))
def test_query_parser_switches_are_not_pinned_in_deploy_templates(profile: str) -> None:
    config = yaml.safe_load(
        (_ROOT / f"deploy/docker/{profile}/config.yml").read_text(encoding="utf-8")
    )
    params = config["memory_api"]["query_parser"]["default"]["params"]

    assert params.keys().isdisjoint(_SWITCHES), (
        f"{profile} 部署模板的 QueryParser 实例 params 会遮蔽 globals: "
        f"{sorted(params.keys() & _SWITCHES)}"
    )
