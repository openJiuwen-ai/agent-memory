"""D14 host layer gate: capability matrix and hook defaults for all hosts."""

from __future__ import annotations

import pytest

from jiuwen_memory_entry.local.hosts import CAPABILITIES, HOOK_DEFAULTS

pytestmark = pytest.mark.integration


def test_only_codex_enables_hooks_by_default():
    assert HOOK_DEFAULTS == {
        "codex": True,
        "dsh": False,
        "hermes": False,
        "workbuddy": False,
    }


@pytest.mark.parametrize("host", ["codex", "dsh", "hermes", "workbuddy"])
def test_every_host_has_mcp_and_capability_row(host):
    row = CAPABILITIES[host]
    assert row.mcp == "supported"
    assert row.memory_import in {"supported", "unsupported"}


def test_host_specific_unverified_capabilities_are_explicit():
    assert CAPABILITIES["dsh"].memory_import == "unsupported"
    assert CAPABILITIES["hermes"].session_import == "unverified"
    assert CAPABILITIES["workbuddy"].auto_recall == "unverified"
    assert CAPABILITIES["workbuddy"].auto_capture == "unverified"
    assert CAPABILITIES["workbuddy"].session_import == "unsupported"


def test_workbuddy_enterprise_accepts_project_root_mcp_file(tmp_path, monkeypatch):
    from jiuwen_memory_entry.local.hosts.workbuddy import detect_workbuddy

    enterprise = tmp_path / "codebuddy"
    enterprise.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JIUWEN_WORKBUDDY_HOME", str(tmp_path / "absent"))
    monkeypatch.setenv("JIUWEN_CODEBUDDY_HOME", str(enterprise))
    detection = detect_workbuddy(project)
    assert detection.edition == "enterprise"
    assert detection.mcp_paths["project"] == project / ".mcp.json"
