"""online 部署配置的关键检索拓扑。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config.context import AssemblyContext
from config.defaults import default_context

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]


def _online_context() -> AssemblyContext:
    payload = yaml.safe_load(
        (_ROOT / "deploy/docker/online/config.yml").read_text(encoding="utf-8")
    )["memory_api"]
    return default_context().merged(AssemblyContext.from_dict(payload))


def test_online_profile_uses_persistent_layer_stores() -> None:
    """L0/L1 与 L2 应使用同类真后端，不能隐式回落到内存 store。"""
    ctx = _online_context()

    fulltext_specs = [
        ctx.lookup("fulltext_store", name) for name in ("default", "layers_l0", "layers_l1")
    ]
    vector_specs = [
        ctx.lookup("vector_store", name) for name in ("default", "layers_l0", "layers_l1")
    ]

    assert {spec.target for spec in fulltext_specs} == {"elasticsearch"}
    assert {spec.target for spec in vector_specs} == {"milvus"}
    assert len({spec.params["index"] for spec in fulltext_specs}) == 3
    assert len({spec.params["collection"] for spec in vector_specs}) == 3
    assert {spec.params["text_analyzer"] for spec in fulltext_specs} == {
        "${ES_TEXT_ANALYZER:-english}"
    }
    assert {spec.params["metric_type"] for spec in vector_specs} == {"COSINE"}


def test_online_profile_keeps_rrf_as_default_fuser() -> None:
    assert _online_context().lookup("fuser", "default").target == "rrf"
