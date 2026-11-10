"""写抑制门闸——正常写入路径与看门狗对账的回环竞态防护（F07 §12.9 风险 6）。

**竞态**：``CompositeStorage`` 文档路径是两步写（md 视图 + 影子索引），两步之间
md 与索引短暂不一致。看门狗 ``_sync_one`` 是 check-then-act（读索引 → 读 md →
diff → 写），在此窗口插入会误判漂移：

- add（先 md 后索引）：「md 有新行、索引没有」→ 看门狗先插幽灵 unit（新 uuid），
  add 的原 unit 随后落地 → 同一 content 两条记忆；
- update/delete（先索引后 md）：「索引已变、md 还是旧的」→ 看门狗删掉刚 update
  的真 unit + 建幽灵（update 丢失），或把刚 delete 的 unit 以新 uuid 复活（双写）。

**防护**：写入编排层在两步写前开启写窗口，在 finally 中关闭；
看门狗只在无写入时读取对账版本，提交 diff 前再验证版本并独占提交窗口。
期间发生过写入则重新对账；提交开始后，新的正常写入等待其结束。

**进程内共享**：写入与看门狗按文档根目录共享门闸。不同根目录独立协调；
同一目录的路径别名共用同一门闸。跨进程不防护。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from weakref import WeakValueDictionary

from jiuwen_memory.common.errors import BackendError

# 看门狗推迟轮询间隔：窗口开着时 sync 以此间隔重查，窗口一关立即续跑。
SYNC_DEFERRAL_POLL_SECONDS = 0.25
# 推迟上限：超过即放弃本次 sync（防写路径 bug 永不关窗卡死任务），记 warning。
# 放弃不丢数据——下一次 md 文件事件会重新触发 sync。
MAX_SYNC_DEFERRAL_SECONDS = 60.0


class _WriteWindowGate:
    """单个文档根目录的写窗口门闸（支持嵌套，线程安全）。"""

    def __init__(self) -> None:
        self._depth = 0
        self._version = 0
        self._committing = False
        self._condition = threading.Condition()

    def open(self) -> None:
        with self._condition:
            if not self._condition.wait_for(
                lambda: not self._committing, timeout=MAX_SYNC_DEFERRAL_SECONDS
            ):
                raise BackendError("timed out waiting for document index synchronization")
            self._depth += 1
            self._version += 1

    def close(self) -> None:
        with self._condition:
            if self._depth > 0:
                self._depth -= 1

    def is_open(self) -> bool:
        with self._condition:
            return self._depth > 0

    def snapshot_version(self) -> int | None:
        with self._condition:
            if self._depth or self._committing:
                return None
            return self._version

    @contextmanager
    def commit_sync(self, version: int) -> Iterator[bool]:
        """只有对账快照仍有效时才允许修改影子索引。"""
        with self._condition:
            accepted = not self._depth and not self._committing and version == self._version
            if accepted:
                self._committing = True
                self._version += 1
        try:
            yield accepted
        finally:
            if accepted:
                with self._condition:
                    self._committing = False
                    self._condition.notify_all()


_gates: WeakValueDictionary[str, _WriteWindowGate] = WeakValueDictionary()
_gates_lock = threading.Lock()


def document_sync_gate(root: str) -> _WriteWindowGate:
    """返回当前进程内该文档根目录的门闸。"""
    path = os.path.normcase(os.path.realpath(root))
    with _gates_lock:
        gate = _gates.get(path)
        if gate is None:
            gate = _gates[path] = _WriteWindowGate()
        return gate
