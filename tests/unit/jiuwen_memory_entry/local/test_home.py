"""Data home, init template and model references (F17 D1 / Task 5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from jiuwen_memory.api import sdk_continuity_runtime_identity
from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory_entry.local.home import (
    MemoryHome,
    ModelSettings,
    init_home,
    resolve_home,
)

pytestmark = pytest.mark.unit

MODEL = ModelSettings(
    llm_base_url="https://api.deepseek.com",
    llm_model="deepseek-v4-pro",
    llm_api_key_ref="env:JIUWEN_MEMORY_LLM_KEY",
    embedder_base_url="https://api.siliconflow.cn/v1",
    embedder_model="BAAI/bge-m3",
    embedder_api_key_ref="JIUWEN_MEMORY_EMBED_KEY",
)


def _initialized(tmp_path) -> tuple[MemoryHome, dict]:
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=MODEL)
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    return home, settings


def test_template_has_no_memory_backends(tmp_path):
    _, settings = _initialized(tmp_path)
    memory = settings["memory_api"]
    for namespace in ("kv_store", "audit", "permission"):
        for name, spec in memory[namespace].items():
            assert spec["target"] != "memory", f"{namespace}.{name} uses an in-memory backend"
            db_path = (spec.get("params") or {}).get("db_path")
            assert db_path != ":memory:", f"{namespace}.{name} uses a transient database"
    assert memory["kv_store"]["registry"]["params"]["fixed_path"] is True
    assert memory["continuity"]["default"]["params"]["registry_store"] == "registry"


def test_template_passes_continuity_identity(tmp_path):
    _, settings = _initialized(tmp_path)
    path, digest = sdk_continuity_runtime_identity(settings["memory_api"])
    assert path and digest, "continuity identity was not derived"


def test_template_carries_local_policies_and_document_mode(tmp_path):
    _, settings = _initialized(tmp_path)
    memory = settings["memory_api"]
    params = memory["continuity"]["default"]["params"]
    assert params["mode"] == "continuity"
    assert params["single_writer"] is True
    assert params["self_bind"] is True
    assert params["admin_manage_tasks"] is True
    assert memory["globals"]["write_document"] is True
    assert memory["globals"]["watch_document"] is True
    assert memory["authenticator"]["default"]["target"] == "local_token"
    assert memory["binding_policy"]["default"]["target"] == "loopback"
    assert settings["local"]["org"] == "local"


def test_resolve_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWEN_MEMORY_HOME", str(tmp_path / "custom"))
    assert resolve_home().root == tmp_path / "custom"
    monkeypatch.delenv("JIUWEN_MEMORY_HOME")
    home = resolve_home()
    if os.name == "nt":
        assert home.root.name == "jiuwen-memory"
    else:
        assert home.root == Path(os.path.expanduser("~/.jiuwen-memory"))


def test_init_refuses_existing(tmp_path):
    home, _ = _initialized(tmp_path)
    with pytest.raises(ConflictError):
        init_home(home, document_root=tmp_path / "docs", model=MODEL)
    init_home(home, document_root=tmp_path / "docs", model=MODEL, force=True)


def test_init_does_not_read_parent_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWEN_MEMORY_API_KEY", "leak")
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=MODEL)
    assert "leak" not in home.service_config.read_text(encoding="utf-8")
    assert "${JIUWEN_MEMORY_LLM_KEY}" in home.service_config.read_text(encoding="utf-8")


def test_init_writes_layout_and_interpreter(tmp_path):
    home, _ = _initialized(tmp_path)
    assert home.service_config.is_file()
    assert home.interpreter_file.is_file()
    assert home.interpreter_file.read_text(encoding="utf-8").strip()
    if os.name != "nt":
        mode = home.service_config.stat().st_mode & 0o777
        assert mode == 0o600, f"service config is not private: {oct(mode)}"


def test_init_keeps_the_data_directory_private(tmp_path):
    home, _ = _initialized(tmp_path)
    if os.name == "nt":
        return
    for directory in (home.root, home.data_dir):
        mode = directory.stat().st_mode & 0o777
        assert mode == 0o700, f"{directory} is not owner-only: {oct(mode)}"


def test_template_marks_remote_components_in_the_egress_report(tmp_path):
    from jiuwen_memory.api import EgressInventory
    from jiuwen_memory.common.embedder.embedder_impl import openai_embedder  # noqa: F401
    from jiuwen_memory.common.llm.llm_impl import openai_llm  # noqa: F401

    _, settings = _initialized(tmp_path)
    rows = EgressInventory.from_settings(settings["memory_api"]).as_list()
    embedder_row = next(row for row in rows if row["component"] == "embedder.default")
    assert embedder_row["destination"] != "local", "remote embedder reported as local"
    assert embedder_row["sends"] == ["document", "query"]
