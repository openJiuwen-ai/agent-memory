"""Public SDK continuity identity inspection without opening runtime resources."""

import os
from copy import deepcopy

import pytest

from jiuwen_memory.api import ValidationError, sdk_continuity_runtime_identity
from jiuwen_memory.common.security.types import SecretValue

pytestmark = pytest.mark.unit


def _config(path):
    return {
        "continuity": {"default": {"target": "local", "params": {
            "mode": "continuity", "single_writer": True, "registry_store": "registry",
        }}},
        "kv_store": {"registry": {"target": "sqlite", "params": {
            "db_path": str(path), "fixed_path": True,
        }}},
    }


def test_identity_normalizes_path_without_mutating_config_or_creating_resources(tmp_path):
    config = _config(tmp_path / "registry.db")
    before = deepcopy(config)
    key, signature = sdk_continuity_runtime_identity(config)
    equivalent = _config(tmp_path / "unused" / ".." / "registry.db")
    assert sdk_continuity_runtime_identity(equivalent) == (key, signature)
    assert key == os.path.normcase(str(tmp_path / "registry.db"))
    assert config == before
    assert not any(tmp_path.iterdir())


def test_identity_includes_secret_values_in_digest_without_returning_them(tmp_path):
    config = _config(tmp_path / "registry.db")
    config["globals"] = {"llm_api_key": "test-secret-one"}
    plain = sdk_continuity_runtime_identity(config)
    config["globals"]["llm_api_key"] = SecretValue("test-secret-one")
    assert sdk_continuity_runtime_identity(config) == plain
    config["globals"]["llm_api_key"] = SecretValue("test-secret-two")
    changed = sdk_continuity_runtime_identity(config)
    assert changed[0] == plain[0]
    assert changed[1] != plain[1]
    assert "test-secret" not in repr((plain, changed))


@pytest.mark.parametrize("options", [
    {"mode": "role"}, {"single_writer": False}, {"allow_ephemeral": True},
])
def test_identity_rejects_unsupported_sharing_config(tmp_path, options):
    config = _config(tmp_path / "registry.db")
    config["continuity"]["default"]["params"].update(options)
    with pytest.raises(ValidationError):
        sdk_continuity_runtime_identity(config)
    assert not any(tmp_path.iterdir())
