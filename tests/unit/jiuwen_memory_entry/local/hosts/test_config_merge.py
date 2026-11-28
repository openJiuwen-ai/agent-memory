"""Shared host-config merge helpers must not treat a broken file as empty."""

from __future__ import annotations

import pytest

from jiuwen_memory_entry.local.hosts import _read_json

pytestmark = pytest.mark.unit


def test_missing_file_reads_as_empty(tmp_path):
    assert _read_json(tmp_path / "absent.json") == {}


def test_broken_file_is_an_error_not_an_empty_config(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": ', encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        _read_json(path)


def test_non_object_config_is_an_error(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        _read_json(path)
