"""角色档装配校验：缺 graph_store 必须启动失败；未配 continuity 必须报能力不支持。"""

import pathlib

import pytest
import yaml

from jiuwen_memory.api import Scope, assemble_runtime
from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError

pytestmark = pytest.mark.unit

_EXAMPLE = pathlib.Path("examples/role_memory/server.yaml")


def _config(tmp_path, *, mode="role", drop=(), drop_section=False):
    cfg = yaml.safe_load(_EXAMPLE.read_text())
    for name, spec in cfg["kv_store"].items():
        spec.setdefault("params", {})["db_path"] = str(tmp_path / f"{name}.db")
    # permission 也是 KV 端口：不重定向会在工作目录落地 role-permissions.db。
    cfg["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    if drop_section:
        cfg.pop("continuity", None)
        return cfg
    params = cfg["continuity"]["default"]["params"]
    params["mode"] = mode
    for key in drop:
        params.pop(key, None)
    return cfg


def test_role_mode_without_graph_keys_fails_at_assembly(tmp_path):
    """两个图键都漏配时不能静默合成易失内存图。"""
    cfg = _config(tmp_path, drop=("graph_store", "relation_index_builder"))
    with pytest.raises(ValidationError) as caught:
        assemble_runtime(config=cfg)
    assert "explicit graph_store" in str(caught.value)


def test_role_mode_missing_graph_store_names_the_missing_key(tmp_path):
    """只漏 graph_store 时，文案要指向漏配的键，而不是“两张图不一致”。"""
    cfg = _config(tmp_path, drop=("graph_store",))
    with pytest.raises(ValidationError) as caught:
        assemble_runtime(config=cfg)
    message = str(caught.value)
    assert "explicit graph_store" in message
    assert "share one GraphStore" not in message


def test_role_mode_with_explicit_graph_store_assembles(tmp_path):
    runtime = assemble_runtime(config=_config(tmp_path))
    try:
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable=protected-access
        assert runtime.api._continuity.health()["continuity_mode"] == "role"
    finally:
        runtime.close()


def test_continuity_api_without_configuration_reports_capability(tmp_path):
    """未配 continuity 时接续入口要报能力不支持，不能被参数个数错误吞掉。"""
    runtime = assemble_runtime(config=_config(tmp_path, drop_section=True))
    try:
        with pytest.raises(UnsupportedCapabilityError) as caught:
            # White-box fixture/fault injection; do not expose a production test API.
            # pylint: disable=protected-access
            runtime.api._role_service(Scope(org="demo", space="role-project"))
        assert not isinstance(caught.value, TypeError)
        assert caught.value.capability == "continuity"
        assert caught.value.value == "unconfigured"
    finally:
        runtime.close()


def test_allow_ephemeral_alone_does_not_waive_the_graph(tmp_path):
    """allow_ephemeral 的语义是「Registry 可易失」，不能顺带放行易失图。"""
    cfg = _config(tmp_path, drop=("graph_store", "relation_index_builder"))
    cfg["continuity"]["default"]["params"]["allow_ephemeral"] = True
    with pytest.raises(ValidationError) as caught:
        assemble_runtime(config=cfg)
    assert "explicit graph_store" in str(caught.value)


def test_allow_ephemeral_graph_does_not_drop_the_registry_writer_lock(tmp_path):
    """图开关只影响图，Registry 的持久性与单写者保护必须保留。"""
    cfg = _config(tmp_path)
    cfg["continuity"]["default"]["params"]["allow_ephemeral_graph"] = True
    runtime = assemble_runtime(config=cfg)
    try:
        # White-box fixture/fault injection; do not expose a production test API.
        # pylint: disable=protected-access
        assert runtime.api._continuity.repository._claimed is True
    finally:
        runtime.close()
