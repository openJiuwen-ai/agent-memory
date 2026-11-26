"""Local-only model source resolution tests."""

from __future__ import annotations

import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common._support import resolve_effective_fp16, resolve_model_source
from jiuwen_memory.common.errors import BackendError

pytestmark = pytest.mark.unit


def test_local_directory_is_returned_without_importing_huggingface_hub(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    assert resolve_model_source(str(tmp_path), component="test") == str(tmp_path)


def test_repo_id_resolves_existing_cache_only(tmp_path, monkeypatch):
    snapshot_download = Mock(return_value=tmp_path)
    module = ModuleType("huggingface_hub")
    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    result = resolve_model_source("BAAI/bge-m3", component="embedder")

    assert result == str(tmp_path)
    snapshot_download.assert_called_once_with(repo_id="BAAI/bge-m3", local_files_only=True)


def test_missing_huggingface_hub_dependency_raises_backend_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    with pytest.raises(BackendError, match="huggingface_hub"):
        resolve_model_source("BAAI/bge-m3", component="embedder")


def test_missing_local_cache_raises_backend_error_without_online_retry(monkeypatch):
    snapshot_download = Mock(side_effect=OSError("local cache miss"))
    module = ModuleType("huggingface_hub")
    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    with pytest.raises(BackendError, match="not available locally"):
        resolve_model_source("BAAI/bge-m3", component="embedder")

    snapshot_download.assert_called_once_with(repo_id="BAAI/bge-m3", local_files_only=True)


def test_resolve_effective_fp16_false_stays_false_without_probing_torch(monkeypatch, caplog):
    cuda_probe = Mock(return_value=True)
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=cuda_probe))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with caplog.at_level(logging.WARNING):
        result = resolve_effective_fp16(False, component="test")

    assert result is False, "explicit use_fp16=False must stay False even when CUDA is available"
    cuda_probe.assert_not_called()
    assert not caplog.records, "use_fp16=False should not emit fallback warnings"


def test_resolve_effective_fp16_false_does_not_import_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)

    assert resolve_effective_fp16(False, component="test") is False


def test_resolve_effective_fp16_true_respects_cuda_availability(monkeypatch, caplog):
    fake_torch_cpu = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch_cpu)
    with caplog.at_level(logging.WARNING):
        assert resolve_effective_fp16(True, component="test") is False
    assert any("falling back to fp32" in record.message for record in caplog.records)

    fake_torch_cuda = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch_cuda)
    assert resolve_effective_fp16(True, component="test") is True
