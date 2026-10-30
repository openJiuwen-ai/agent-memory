"""ConfigSource：默认 yaml_defaults、可变 dict、overlay、active 解析与 PromptRegistry 晚绑定。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config import Config
from jiuwen_memory.config.active import resolve_active_name, resolve_bound_value
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.config.keys import instance_namespace
from jiuwen_memory.config.project import project_assembly_values
from jiuwen_memory.config.routing import ActiveRouter, RoutingEmbedder
from jiuwen_memory.construction.prompt_registry import PHASE_EXTRACT, PromptRegistry

pytestmark = pytest.mark.unit


def test_instance_namespace_keeps_anonymous_llm_on_shared_keys() -> None:
    assert instance_namespace("llm", "") == "llm"
    assert instance_namespace("llm", "default") == "llm"
    assert instance_namespace("llm", "alt") == "llm.alt"


def test_project_assembly_values_includes_globals_and_prompts() -> None:
    ctx = default_context()
    ctx.globals["vector_enabled"] = False
    ctx.globals["prompts"] = {"extract": {"episodic": "抽取事件"}}
    values = project_assembly_values(ctx)
    assert values["globals.vector_enabled"] == "false"
    assert values["prompts.extract.episodic"] == "抽取事件"


def test_project_assembly_values_scopes_instance_keys_to_migrated_namespaces() -> None:
    ctx = default_context().merged(
        AssemblyContext.from_dict(
            {
                "llm": {
                    "default": {
                        "target": "openai",
                        "params": {
                            "llm_model": "default-model",
                            "llm_api_key": "default-key",
                            "llm_base_url": "https://default.example/v1",
                        },
                    },
                    "alt": {
                        "target": "openai",
                        "params": {
                            "llm_model": "alt-model",
                            "llm_api_key": "alt-key",
                            "llm_base_url": "https://alt.example/v1",
                        },
                    },
                },
                "embedder": {
                    "default": {
                        "target": "openai",
                        "params": {"embedder_api_key": "default-embed-key"},
                    },
                    "main": {
                        "target": "openai",
                        "params": {"embedder_api_key": "main-embed-key"},
                    },
                },
            }
        )
    )

    values = project_assembly_values(ctx)

    assert values["llm.model"] == "default-model"
    assert values["llm.api_key"] == "default-key"
    assert values["llm.base_url"] == "https://default.example/v1"
    assert values["llm.alt.model"] == "alt-model"
    assert values["llm.alt.api_key"] == "alt-key"
    assert values["llm.alt.base_url"] == "https://alt.example/v1"
    assert "llm.alt.llm_model" not in values
    assert values["embedder.api_key"] == "default-embed-key"
    assert values["embedder.main.embedder_api_key"] == "main-embed-key"
    assert "embedder.main.api_key" not in values


def test_yaml_defaults_config_source_fetch_roundtrip() -> None:
    from jiuwen_memory.config.config_source_impl.yaml_defaults_config_source import (
        YamlDefaultsConfigSource,
    )

    src = YamlDefaultsConfigSource({"globals.rerank_enabled": "true", "llm.model": "gpt-4o"})
    assert src.fetch("globals.rerank_enabled") == "true"
    assert src.fetch("llm.model") == "gpt-4o"
    assert src.fetch("missing.key") is None
    src.health()


def test_dict_config_source_supports_runtime_update() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    src = DictConfigSource({"embedder.active": "main"})
    assert src.fetch("embedder.active") == "main"
    src.put("embedder.active", "backup")
    assert src.fetch("embedder.active") == "backup"


def test_overlay_prefers_primary_then_fallback() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
    from jiuwen_memory.config.config_source_impl.overlay_config_source import OverlayConfigSource
    from jiuwen_memory.config.config_source_impl.yaml_defaults_config_source import (
        YamlDefaultsConfigSource,
    )

    base = YamlDefaultsConfigSource({"llm.model": "from-yaml-defaults", "llm.api_key": "k1"})
    overlay = DictConfigSource({"llm.model": "from-overlay"})
    src = OverlayConfigSource(primary=overlay, fallback=base)
    assert src.fetch("llm.model") == "from-overlay"
    assert src.fetch("llm.api_key") == "k1"


def test_resolve_active_name_rejects_unknown_instance() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    src = DictConfigSource({"embedder.active": "ghost"})
    with pytest.raises(ValidationError, match="ghost"):
        resolve_active_name(
            src,
            namespace="embedder",
            available=("main", "backup"),
            default="main",
        )


def test_resolve_active_name_uses_default_when_unset() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    src = DictConfigSource({})
    assert (
        resolve_active_name(
            src,
            namespace="embedder",
            available=("main", "backup"),
            default="main",
        )
        == "main"
    )


def test_resolve_bound_value_prefers_live_then_fallback() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    src = DictConfigSource({"llm.model": "gpt-4o"})
    assert (
        resolve_bound_value(src, namespace="llm", field="model", fallback="echo")
        == "gpt-4o"
    )
    assert (
        resolve_bound_value(src, namespace="llm", field="api_key", fallback="sk-local")
        == "sk-local"
    )
    assert resolve_bound_value(None, namespace="llm", field="model", fallback="echo") == "echo"


class _StubEmbedder(Embedder):
    def __init__(self, name: str, dim: int = 8) -> None:
        self.name = name
        self._dim = dim

    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(self.name))] * self._dim for _ in texts]

    def dimension(self) -> int:
        return self._dim


def test_routing_embedder_switches_by_active() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    cfg = DictConfigSource({"embedder.active": "hashing"})
    router = ActiveRouter(
        namespace="embedder",
        instances={"hashing": _StubEmbedder("hashing"), "openai_a": _StubEmbedder("openai_a")},
        config_source=cfg,
        default_name="hashing",
    )
    emb = RoutingEmbedder(router)
    assert emb.embed(["x"])[0][0] == float(len("hashing"))
    cfg.put("embedder.active", "openai_a")
    assert emb.embed(["x"])[0][0] == float(len("openai_a"))


def test_prompt_registry_prefers_config_source() -> None:
    from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

    cfg = DictConfigSource({"prompts.extract.episodic": "来自 ConfigSource"})
    registry = PromptRegistry(
        {"extract": {"episodic": "来自构造期目录"}},
        config_source=cfg,
    )
    assert registry.get(PHASE_EXTRACT, "episodic") == "来自 ConfigSource"
    cfg.put("prompts.extract.episodic", "已切换")
    assert registry.get(PHASE_EXTRACT, "episodic") == "已切换"


def test_build_kernel_exposes_default_config_source() -> None:
    from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
    from jiuwen_memory.config.config_source_impl.yaml_defaults_config_source import (
        YamlDefaultsConfigSource,
    )

    Factory.reset_all()
    kernel = build_kernel()
    assert isinstance(kernel.config_source, ConfigSource)
    assert isinstance(kernel.config_source, YamlDefaultsConfigSource)
    # defaults 投影应能读到 globals 开关
    enabled = kernel.config_source.fetch("globals.vector_enabled")
    assert enabled in {"true", "True", "1"} or enabled == "true"


def test_build_kernel_can_inject_dict_config_source() -> None:
    from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel

    Factory.reset_all()
    cfg = Config.from_dict(
        {
            "config_source": {
                "default": {
                    "target": "dict",
                    "params": {"values": {"embedder.active": "custom"}},
                }
            }
        }
    )
    kernel = build_kernel(config=cfg)
    assert kernel.config_source.fetch("embedder.active") == "custom"
    # dict 源应可运行时更新（产品简易落地）
    put = getattr(kernel.config_source, "put", None)
    assert callable(put)
    put("embedder.active", "other")
    assert kernel.config_source.fetch("embedder.active") == "other"


def test_config_source_producer_registers_yaml_defaults_and_dict() -> None:
    from jiuwen_memory.config.config_source_impl import register_config_sources

    register_config_sources()
    assert "yaml_defaults" in ConfigSourceProducer.known()
    assert "dict" in ConfigSourceProducer.known()
