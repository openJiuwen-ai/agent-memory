"""写抑制门闸——正常写入路径与看门狗对账的回环竞态防护（F07 §12.9 风险 6）。

**竞态**：``CompositeStorage`` 文档路径是两步写（md 视图 + 影子索引），两步之间
md 与索引短暂不一致。看门狗 ``_sync_one`` 是 check-then-act（读索引 → 读 md →
diff → 写），在此窗口插入会误判漂移：

- add（先 md 后索引）：「md 有新行、索引没有」→ 看门狗先插幽灵 unit（新 uuid），
  add 的原 unit 随后落地 → 同一 content 两条记忆；
- update/delete（先索引后 md）：「索引已变、md 还是旧的」→ 看门狗删掉刚 update
  的真 unit + 建幽灵（update 丢失），或把刚 delete 的 unit 以新 uuid 复活（双写）。

**防护**：写入编排层（``CompositeStorage`` 文档路径）在两步写之前 ``open_write_window``、
完成后 ``close_write_window``（try/finally 配对，精确覆盖含慢 embed 的全过程）；
看门狗 sync 入口查 ``write_window_open``——窗口开着则**推迟**本轮对账（轮询等窗口
关闭后重跑，而非丢弃：窗口内用户手改 md 的真实漂移，窗口关闭后的重扫能补上）。

**进程内共享**：模块级单例——composite 与 watchdog 无需互相持有引用，同进程
import 即共享。跨进程不防护（与 LocalMarkdownStore 的并发声明同口径：跨进程
文件锁待后续加）。
"""

from __future__ import annotations

import threading

# 看门狗推迟轮询间隔：窗口开着时 sync 以此间隔重查，窗口一关立即续跑。
SYNC_DEFERRAL_POLL_SECONDS = 0.25
# 推迟上限：超过即放弃本次 sync（防写路径 bug 永不关窗卡死任务），记 warning。
# 放弃不丢数据——下一次 md 文件事件会重新触发 sync。
MAX_SYNC_DEFERRAL_SECONDS = 60.0


class _WriteWindowGate:
    """进程级写窗口计数门闸（open/close 配对，支持嵌套，线程安全）。"""

    def __init__(self) -> None:
        self._depth = 0
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            self._depth += 1

    def close(self) -> None:
        with self._lock:
            if self._depth > 0:
                self._depth -= 1

    def is_open(self) -> bool:
        with self._lock:
            return self._depth > 0


# 模块级单例：composite（生产者）与 watchdog（消费者）import 同一对象。
SYNC_GATE = _WriteWindowGate()


def open_write_window() -> None:
    """写入编排开始：挡住看门狗对账，直至 close_write_window（F07 §12.9 风险 6）。"""
    SYNC_GATE.open()


def close_write_window() -> None:
    """写入编排完成（或异常退出）：放行看门狗对账。"""
    SYNC_GATE.close()


def write_window_open() -> bool:
    """写入编排是否在途（窗口开着时看门狗应推迟 sync）。"""
    return SYNC_GATE.is_open()
