"""写抑制门闸（``storage.sync_gate``）——正常写入与看门狗对账的回环竞态防护。

``_WriteWindowGate`` 是 open/close 配对的进程级计数门闸：正常写入路径在「md 视图 +
影子索引」两步写之间 ``open_write_window`` / ``close_write_window``（try/finally 配对），
看门狗 sync 入口查 ``write_window_open`` 决定是否推迟对账。失效方向：

- close 计数不配对（少 close 或多 close）→ 门闸恒开（看门狗永久推迟）或恒关（防护失效）。
- 嵌套写路径需要深度计数而非布尔位——布尔位会在内层 close 时提前放行，让看门狗插进
  外层的两步写窗口。

故本文件锁死：嵌套配对、close 不把深度降到负、模块级单例初始为关闭。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.storage.sync_gate import (
    SYNC_GATE,
    _WriteWindowGate,
    close_write_window,
    open_write_window,
    write_window_open,
)

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


def test_module_singleton_functions_share_one_gate() -> None:
    """composite（生产者）与 watchdog（消费者）import 同一模块级单例。"""
    assert write_window_open() is False
    open_write_window()
    try:
        assert write_window_open() is True
        assert SYNC_GATE.is_open() is True
    finally:
        close_write_window()
    assert write_window_open() is False
