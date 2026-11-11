# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Maintenance CLI accepts existing deployment YAML and explains malformed input."""

import json
from pathlib import Path

import pytest
import yaml

from deploy.migration.encrypt import _load_config as load_migration_config
from deploy.migration.encrypt import main

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("profile", ["local", "online", "postgres"])
def test_load_repository_service_configuration(profile):
    path = ROOT / "deploy" / "docker" / profile / "config.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    context = load_migration_config(str(path)).context()
    assert "profile" not in context.namespaces
    assert "memory_api" not in context.namespaces
    assert context.namespaces["kv_store"]["default"].target == (
        data["memory_api"]["kv_store"]["default"]["target"]
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_load_expands_selected_kernel_section_and_ignores_outer_settings(
    tmp_path, monkeypatch, wrapped
):
    data = {"globals": {"fixture": "${MIGRATION_CONFIG_FIXTURE:-fallback}"}}
    if wrapped:
        data = {
            "profile": "${OUTER_UNUSED:-${NESTED_UNUSED}}",
            "globals": {"fixture": "outer must not override kernel"},
            "memory_api": data,
        }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("MIGRATION_CONFIG_FIXTURE", "selected-value")
    assert load_migration_config(str(path)).context().globals["fixture"] == "selected-value"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", "YAML mapping"),
        ("- secret-sentinel", "YAML mapping"),
        ("profile: docker\nmemory_api: null", "memory_api must be a mapping"),
        ("profile: docker\nmemory_api: [secret-sentinel]", "memory_api must be a mapping"),
        ("profile: docker\nmemory_api: secret-sentinel", "memory_api must be a mapping"),
        ("profile: docker", "requires a memory_api mapping"),
        ("memory_api: [secret-sentinel", "line 1, column"),
        ("globals:\n  fixture: '${TOKEN:-${NESTED}}'", "Invalid configuration"),
        ("globals: [secret-sentinel]", "Invalid kernel configuration layout"),
    ],
)
def test_cli_rejects_bad_configuration_with_safe_diagnostic(
    tmp_path, monkeypatch, capsys, text, expected
):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    def unexpected_maintenance(*args, **kwargs):
        pytest.fail("invalid configuration must not assemble or touch storage")

    monkeypatch.setattr(
        "deploy.migration.encrypt.migrate_plaintext_memories", unexpected_maintenance
    )
    assert main(["--config", str(path), "--operation", "inspect"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "failed" and body["error"] == "invalid_input"
    assert expected in body["message"]
    assert "secret-sentinel" not in body["message"]
    assert str(path) not in body["message"]


@pytest.mark.parametrize("missing", ["config", "input"])
def test_cli_reports_unreadable_file_without_traceback(tmp_path, capsys, missing):
    path = tmp_path / "config.yaml"
    arguments = ["--config", str(path), "--operation", "inspect"]
    if missing == "input":
        path.write_text("{}", encoding="utf-8")
        arguments.extend(["--input", str(tmp_path / "missing.jsonl")])
    assert main(arguments) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "failed" and body["error"] == "input_failed"
    assert "file existence and permissions" in body["message"]
