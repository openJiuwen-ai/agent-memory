"""覆盖文档写窗口的嵌套计数与目录隔离。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.storage import sync_gate
from jiuwen_memory.storage.sync_gate import _WriteWindowGate, document_sync_gate

pytestmark = pytest.mark.unit


def test_a_gate_starts_closed() -> None:
    assert _WriteWindowGate().is_open() is False


def test_open_close_pairing_tracks_depth() -> None:
    gate = _WriteWindowGate()
    gate.open()
    assert gate.is_open() is True
    gate.close()
    assert gate.is_open() is False


def test_nested_windows_require_matched_close() -> None:
    """嵌套写路径：内层 close 不得提前放行外层窗口（布尔位会在此误放行）。"""
    gate = _WriteWindowGate()
    gate.open()
    gate.open()
    gate.close()  # 内层关
    assert gate.is_open() is True  # 外层仍开
    gate.close()  # 外层关
    assert gate.is_open() is False


def test_close_never_goes_negative() -> None:
    """多余 close 不把深度降到负（否则下一次 open 后深度仍可能错乱）。"""
    gate = _WriteWindowGate()
    gate.close()
    gate.close()
    assert gate.is_open() is False
    gate.open()
    assert gate.is_open() is True


def test_document_gates_share_path_aliases_but_isolate_roots(tmp_path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    gate = document_sync_gate(str(root))
    same_root = document_sync_gate(str(alias))
    other_root = document_sync_gate(str(tmp_path / "other"))
    assert same_root is gate
    gate.open()
    try:
        assert same_root.is_open()
        assert not other_root.is_open()
    finally:
        gate.close()
    assert not same_root.is_open()


def test_write_fails_if_index_sync_does_not_release_its_window(monkeypatch) -> None:
    monkeypatch.setattr(sync_gate, "MAX_SYNC_DEFERRAL_SECONDS", 0)
    gate = _WriteWindowGate()
    with gate.commit_sync(gate.snapshot_version()) as accepted:
        assert accepted
        with pytest.raises(BackendError, match="timed out"):
            gate.open()
        assert not gate.is_open()
    gate.open()
    assert gate.is_open()
    gate.close()
