"""单次运行时绑定持久配置与 Prompt 视图。"""

from unittest.mock import Mock

import pytest

from jiuwen_memory.api import ManagedConfiguration, assemble_managed_runtime
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore
from jiuwen_memory.construction.prompt_registry import PromptRegistry

pytestmark = pytest.mark.unit


def test_named_model_config_survives_restart_and_preserves_prompt(tmp_path, managed_models):
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    model_key = "llm.alt.params.llm_model"
    security = object()
    config = {
        "llm": {"alt": {"target": "echo", "params": {
            "llm_model": "base-model", "config_namespace": "special",
        }}},
        "config_source": {"default": {"target": "yaml_defaults", "params": {
            "values": {"special.model": "source-model"},
        }}},
        "extractor": {"default": {"target": "dynamic_llm", "params": {"llm": "alt"}}},
        "embedder": {"default": {"target": "hashing", "params": {"embedder_dim": 32}}},
        "prompts": {"extract": {"fact": "old prompt"}},
    }

    def assemble():
        return assemble_managed_runtime(
            config, store=store, llm_instances=["alt"],
            authorize=lambda security, op: True,
        )

    runtime = assemble()
    try:
        source = managed_models[-1].source
        registry = PromptRegistry(config_source=source)
        assert source.fetch("special.model") == "source-model"
        assert registry.get("extract", "fact") == "old prompt"
        info = runtime.configuration.read(security=security)["instance"]
        assert info["embedding_dimension"] == 32
        result = runtime.configuration.save(0, {model_key: "saved-model"}, security=security)
        assert result == {"version": 1, "applied": True}
        assert source.fetch("special.model") == "saved-model"
        assert registry.get("extract", "fact") == "old prompt"
    finally:
        runtime.close(wait=True)
    restarted = assemble()
    try:
        source = managed_models[-1].source
        assert source.fetch("special.model") == "saved-model"
        registry = PromptRegistry(config_source=source)
        assert registry.get("extract", "fact") == "old prompt"
        result = restarted.configuration.save(1, {}, restore=[model_key], security=security)
        assert result == {"version": 2, "applied": True}
        assert source.fetch("special.model") == "source-model"
        assert registry.get("extract", "fact") == "old prompt"
    finally:
        restarted.close(wait=True)


def test_managed_configuration_requires_host_authorization(tmp_path):
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    runtime = assemble_managed_runtime(
        store=store, llm_instances=["default"],
        authorize=lambda security, op: False,
    )
    try:
        with pytest.raises(PermissionDeniedError):
            runtime.configuration.read(security=object())
        with pytest.raises(PermissionDeniedError):
            runtime.configuration.save(0, {"llm.default.target": "echo"}, security=object())
        assert store.read().version == 0
    finally:
        runtime.close(wait=True)


@pytest.mark.parametrize("decision", [False, None])
def test_managed_configuration_denied_actions_do_not_call_service(decision):
    service = Mock()
    gate = ManagedConfiguration(service, lambda security, operation: decision)
    with pytest.raises(PermissionDeniedError):
        gate.read(security=object())
    with pytest.raises(PermissionDeniedError):
        gate.save(0, {"x": 1}, security=object())
    with pytest.raises(PermissionDeniedError):
        gate.retry_publication(security=object())
    assert service.mock_calls == []


def test_plaintext_overlay_rejected_with_storage_encryption(tmp_path):
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    with pytest.raises(ValidationError, match="managed config overlay"):
        assemble_managed_runtime(
            {"globals": {"storage_encryption_enabled": True}},
            store=store, llm_instances=["default"],
            authorize=lambda security, op: True,
        )


def test_assembly_rejects_configuration_changed_during_startup(tmp_path):
    from jiuwen_memory.common.errors import ConflictError

    real = SQLiteOverlayStore(tmp_path / "settings.db")

    class ChangingStore:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            if self.reads == 2:
                real.commit(0, {"prompts.extract.fact": "new"})
            return real.read()

        @staticmethod
        def commit(expected_version, values):
            return real.commit(expected_version, values)

    with pytest.raises(ConflictError, match="changed during runtime assembly"):
        assemble_managed_runtime(
            {"prompts": {"extract": {"fact": "old"}}},
            store=ChangingStore(), llm_instances=["default"],
            authorize=lambda security, op: True,
        )


def test_unsupported_historical_override_is_preserved_but_not_applied(tmp_path):
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    store.commit(0, {"llm.alt.params.llm_model": "old-user-value"})
    config = {"llm": {"alt": {"target": "unknown_plugin"}}}
    runtime = assemble_managed_runtime(
        config, store=store, llm_instances=["default", "alt"],
        authorize=lambda security, operation: True,
    )
    try:
        view = runtime.configuration.read(security=object())
        assert view["ignored_overrides"] == 1
        assert not any(field["key"].startswith("llm.alt.") for field in view["fields"])
        runtime.configuration.save(
            1, {"llm.default.params.llm_model": "new"}, security=object()
        )
        assert store.read().values["llm.alt.params.llm_model"] == "old-user-value"
    finally:
        runtime.close(wait=True)
