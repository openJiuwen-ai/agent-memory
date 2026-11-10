# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""同进程文档资源协调：完整写入/补偿与看门狗对账共享根目录、索引锁。

锁在读取旧状态之前获取，覆盖文件与索引的整个操作。相同根目录或相同索引
必须串行，两者均独立则可并行。写窗口仅用于看门狗异步推迟，互斥由资源锁保证。
锁按稳定顺序获取；注册表只保护锁登记/计数，不在全局锁内执行 IO。
不协调其他进程或外部编辑器，不自动修复已有重复索引。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from jiuwen_memory.common.errors import BackendError

SYNC_DEFERRAL_POLL_SECONDS = 0.25
MAX_SYNC_DEFERRAL_SECONDS = 60.0


@dataclass
class _ResourceGate:
    """一个资源的可重入互斥锁与在途写入计数。"""

    lock: threading.RLock = field(default_factory=threading.RLock)
    writers: int = 0


# 在途/等待操作持有强引用；全部退出后回收，避免动态目录无限累积。
_GATES: WeakValueDictionary[str, _ResourceGate] = WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def resource_path_key(kind: str, path: str) -> str:
    """归一绝对路径、相对路径和符号链接，令同一物理资源共用锁。"""
    return f"{kind}:{os.path.normcase(os.path.realpath(path or '.'))}"


def _resource_keys(markdown_root: str, shadow_key: str) -> list[str]:
    return sorted({resource_path_key("markdown", markdown_root), shadow_key})


@contextmanager
def document_operation(
    markdown_root: str, shadow_key: str, *, writing: bool = False,
) -> Iterator[None]:
    """同步操作边界：先锁资源，再读旧状态，成功或完成补偿后释放。

    不跨 await 使用；资源锁始终先于各存储实例自身的锁获取。
    获取整组锁共用超时预算；超时释放已取得的锁并抛 BackendError。
    writing=True 表示程序写窗口，看门狗对账使用默认值。
    """
    keys = _resource_keys(markdown_root, shadow_key)
    with _REGISTRY_LOCK:
        gates = []
        for key in keys:
            gate = _GATES.get(key)
            if gate is None:
                gate = _ResourceGate()
                _GATES[key] = gate
            gates.append(gate)
    deadline = time.monotonic() + MAX_SYNC_DEFERRAL_SECONDS
    with ExitStack() as stack:
        for gate in gates:
            timeout = max(0.0, deadline - time.monotonic())
            if not gate.lock.acquire(timeout=timeout):
                raise BackendError("timed out waiting for document resource lock")
            stack.callback(gate.lock.release)
        if writing:
            with _REGISTRY_LOCK:
                for gate in gates:
                    gate.writers += 1
        try:
            yield
        finally:
            if writing:
                with _REGISTRY_LOCK:
                    for gate in gates:
                        gate.writers -= 1


def write_window_open(markdown_root: str | None = None, shadow_key: str = "") -> bool:
    """指定资源是否正在写入；省略资源仅用于诊断所有在途写窗口。"""
    keys = None if markdown_root is None else _resource_keys(markdown_root, shadow_key)
    with _REGISTRY_LOCK:
        if keys is None:
            gates = tuple(_GATES.values())
        else:
            gates = tuple(
                gate for key in keys
                if (gate := _GATES.get(key)) is not None
            )
        return any(gate.writers for gate in gates)
