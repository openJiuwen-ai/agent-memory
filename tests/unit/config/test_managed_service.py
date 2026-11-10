"""Model persistence/apply contracts; original Prompt behavior stays outside this feature."""

import logging
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import assemble_managed_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.managed_fields import ManagedFields
from jiuwen_memory.config.managed_service import ManagedConfigService
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

pytestmark = pytest.mark.unit


def create(path, *, locked=frozenset()):
    return assemble_managed_runtime(
        {"llm": {"default": {"target": "echo", "params": {"llm_model": "base"}}}},
        store=SQLiteOverlayStore(path),
        llm_instances=["default"],
        authorize=lambda security, operation: True,
        locked_keys=locked,
    )


def test_host_lock_overrides_previous_saved_value(tmp_path):
    path = tmp_path / "settings.db"
    SQLiteOverlayStore(path).commit(0, {"llm.default.params.llm_model": "saved"})
    runtime = create(path, locked=frozenset({"llm.default.params.llm_model"}))
    try:
        view = runtime.configuration.read(security=object())
        field = next(f for f in view["fields"] if f["key"].endswith(".llm_model"))
        assert field["effective"] == "base" and not field["writable"]
        with pytest.raises(ValidationError):
            runtime.configuration.save(1, {field["key"]: "bad"}, security=object())
    finally:
        runtime.close()


def test_save_cannot_bypass_existing_tls_policy(tmp_path):
    base = AssemblyContext.from_dict(
        {
            "globals": {"llm_ssl_verify": True},
            "llm": {
                "main": {"target": "openai", "params": {"llm_base_url": "https://safe.example/v1"}}
            },
        }
    )
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    service = ManagedConfigService(base, ManagedFields(base, ["main"]), store)
    with pytest.raises(ValidationError):
        service.save(0, {
            "llm.main.params.llm_base_url": "http://other.example/v1",
            "llm.main.params.llm_api_key": "new-key",
            "llm.main.params.llm_model": "new-model",
        })
    assert store.read().version == 0


def test_credential_field_is_read_only_without_protected_store():
    from jiuwen_memory.config.overlay_store import OverlaySnapshot

    class MemoryStore:
        def __init__(self):
            self.snapshot = OverlaySnapshot(0, {})

        def read(self):
            return self.snapshot

        def commit(self, expected_version, values):
            self.snapshot = OverlaySnapshot(expected_version + 1, dict(values))
            return self.snapshot

    base = AssemblyContext.from_dict({"llm": {"default": "echo"}})
    service = ManagedConfigService(base, ManagedFields(base, ["default"]), MemoryStore())
    view = service.read()
    field = next(item for item in view["fields"] if item["key"].endswith("llm_api_key"))
    assert field["writable"] is False
    assert "凭据" in field["reason"]
    with pytest.raises(ValidationError):
        service.save(0, {"llm.default.params.llm_api_key": "synthetic-secret"})


def test_save_apply_failure_retry_and_no_version_regression(
    tmp_path, caplog, monkeypatch, managed_models,
):
    runtime = create(tmp_path / "settings.db")
    logger = logging.getLogger("agent_memory.config.managed_service")
    logger.addHandler(caplog.handler)
    try:
        source = managed_models[-1].source
        with monkeypatch.context() as patch:
            failure = RuntimeError("synthetic-sensitive-error")
            patch.setattr(source, "apply", Mock(side_effect=failure))
            result = runtime.configuration.save(
                0, {"llm.default.params.llm_model": "new"}, security=object()
            )
        assert result == {"version": 1, "applied": False}
        assert "version=1 error_type=RuntimeError" in caplog.text
        assert "synthetic-sensitive-error" not in caplog.text
        assert runtime.configuration.read(security=object())["applied_version"] == 0
        assert runtime.configuration.retry_publication(security=object())["applied"]
        assert not source.apply(source.context, 0)
        assert source.version == 1
    finally:
        logger.removeHandler(caplog.handler)
        runtime.close()


@pytest.mark.parametrize("target", ["echo", "openai"])
@pytest.mark.parametrize("thinking", [True, False])
def test_restore_provider_removes_dashscope_only_overlay(tmp_path, target, thinking):
    store = SQLiteOverlayStore(tmp_path / "restore.db")
    runtime = assemble_managed_runtime(
        {"llm": {"default": {"target": target}}},
        store=store, llm_instances=["default"], authorize=lambda *_: True,
    )
    try:
        config = runtime.configuration
        endpoint = {
            "llm.default.params.llm_base_url": "https://dashscope.invalid/v1",
            "llm.default.params.llm_api_key": "dashscope-key",
            "llm.default.params.llm_model": "qwen-plus",
        }
        assert config.save(0, {
            **endpoint, "llm.default.target": "dashscope",
            "llm.default.params.enable_thinking": thinking,
        }, security=object())["applied"]
        assert config.save(
            1, {}, restore=["llm.default.target", *endpoint], security=object()
        )["applied"]
        assert "llm.default.params.enable_thinking" not in store.read().values
        view = config.read(security=object())
        provider = next(field for field in view["fields"] if field["key"] == "llm.default.target")
        assert provider["effective"] == target
        assert not provider["has_override"]
    finally:
        runtime.close()


def test_service_switch_requires_complete_endpoint_and_preserves_unmanaged_values(
    tmp_path, managed_models,
):
    path = tmp_path / "settings.db"
    store = SQLiteOverlayStore(path)
    store.commit(0, {"prompts.extract.fact": "keep existing value"})
    runtime = assemble_managed_runtime(
        {"llm": {"default": {"target": "openai", "params": {
            "llm_base_url": "https://old.invalid/v1", "llm_api_key": "old-key",
            "llm_model": "old-model",
        }}}},
        store=store, llm_instances=["default"], authorize=lambda *_: True,
    )
    url = "llm.default.params.llm_base_url"
    key = "llm.default.params.llm_api_key"
    name = "llm.default.params.llm_model"
    try:
        config = runtime.configuration
        assert config.save(1, {url: "https://OLD.invalid:443/v2", name: "same-service"},
                           security=object())["applied"]
        with pytest.raises(ValidationError, match="model URL is invalid"):
            config.save(2, {url: "https://new.invalid:65536/v1", key: "new-key"}, security=object())
        for changes in (
            {url: "https://new.invalid/v1"},
            {url: "https://new.invalid/v1", key: "new-key"},
            {url: "https://new.invalid/v1", name: "new-model", key: ""},
        ):
            with pytest.raises(ValidationError, match="服务网址、API Key 和模型名"):
                config.save(2, changes, security=object())
            assert store.read().version == 2
        assert config.save(2, {
            url: "https://new.invalid/v1", name: "new-model", key: "new-key",
        }, security=object())["applied"]
        source = managed_models[-1].source
        assert source.fetch("llm.api_key") == "new-key"
        with pytest.raises(ValidationError, match="服务网址、API Key 和模型名"):
            config.save(3, {}, restore=[key], security=object())
        assert config.save(3, {}, restore=[url, key, name], security=object())["applied"]
        assert source.fetch("llm.api_key") == "old-key"
        assert store.read().values == {"prompts.extract.fact": "keep existing value"}
    finally:
        runtime.close()


@pytest.mark.parametrize("change", ["host", "locked_key", "legacy_url"])
def test_restart_uses_startup_endpoint_when_saved_group_is_unsafe(
    tmp_path, monkeypatch, managed_models, change,
):
    from jiuwen_memory.config import managed_service

    log = Mock()
    monkeypatch.setattr(managed_service, "_LOG", log)
    store = SQLiteOverlayStore(tmp_path / "config.db")
    key = "llm.default.params.llm_api_key"
    url = "llm.default.params.llm_base_url"
    name = "llm.default.params.llm_model"
    timeout = "llm.default.params.llm_timeout"

    def build(origin, *, locked=frozenset()):
        return assemble_managed_runtime(
            {"llm": {"default": {"target": "openai", "params": {
                "llm_base_url": origin, "llm_api_key": "startup-key", "llm_model": "base",
            }}}},
            store=store, llm_instances=["default"], authorize=lambda *_: True, locked_keys=locked,
        )

    first, second = "https://first.invalid/v1", "https://second.invalid/v1"
    if change == "legacy_url":
        store.commit(0, {url: second, timeout: 17})
    else:
        runtime = build(first)
        try:
            changes = {key: "saved-key", timeout: 17}
            if change == "locked_key":
                changes.update({url: second, name: "new-model"})
            assert runtime.configuration.save(0, changes, security=object())["applied"]
        finally:
            runtime.close()
    saved = store.read()
    locked = frozenset({key}) if change == "locked_key" else frozenset()
    startup_url = second if change == "host" else first
    restarted = build(startup_url, locked=locked)
    try:
        source = managed_models[-1].source
        assert source.fetch("llm.api_key") == "startup-key"
        assert source.fetch("llm.base_url") == startup_url
        assert source.context.lookup("llm", "default").params["llm_timeout"] == 17
        view = restarted.configuration.read(security=object())
        assert view["ignored_endpoint_fields"]
        log.warning.assert_called_once_with(
            "Portal model endpoint settings skipped: fields=%s", view["ignored_endpoint_fields"]
        )
        assert store.read() == saved
        if change == "host":
            assert restarted.configuration.save(
                saved.version, {key: "replacement-key"}, security=object()
            )["applied"]
            assert source.fetch("llm.api_key") == "replacement-key"
    finally:
        restarted.close()


@pytest.mark.parametrize(("provider", "temperature", "valid"), [
    ("openai", -1, False), ("openai", 2, True), ("openai", 1e9, False),
    ("dashscope", 1.99, True), ("dashscope", 2, False),
])
def test_portal_temperature_range_matches_provider(tmp_path, provider, temperature, valid):
    store = SQLiteOverlayStore(tmp_path / "config.db")
    runtime = assemble_managed_runtime(
        {"llm": {"default": {"target": provider, "params": {"llm_temperature": 3}}}},
        store=store, llm_instances=["default"], authorize=lambda *_: True,
    )
    try:
        changes = {"llm.default.params.llm_temperature": temperature}
        if valid:
            assert runtime.configuration.save(0, changes, security=object())["applied"]
        else:
            with pytest.raises(ValidationError, match="model numeric value"):
                runtime.configuration.save(0, changes, security=object())
            assert store.read().version == 0
    finally:
        runtime.close()


def test_provider_switch_checks_saved_temperature_against_new_provider(tmp_path):
    runtime = create(tmp_path / "settings.db")
    temperature = "llm.default.params.llm_temperature"
    try:
        config = runtime.configuration
        assert config.save(0, {temperature: 3}, security=object())["applied"]
        changes = {
            "llm.default.target": "openai",
            "llm.default.params.llm_base_url": "https://new.invalid/v1",
            "llm.default.params.llm_api_key": "synthetic-key",
            "llm.default.params.llm_model": "model",
        }
        with pytest.raises(ValidationError, match="model numeric value"):
            config.save(1, changes, security=object())
        assert config.read(security=object())["version"] == 1
        assert config.save(1, {**changes, temperature: 1}, security=object())["applied"]
    finally:
        runtime.close()
