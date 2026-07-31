"""AuditLogger — 审计记录（横切共用接口，架构 §12）。

**共用说明**：透明可治理是一等公民——写入/修改/遗忘/检索/授权等关键
动作需要留痕，且事件产生在**各层**：接入层（写入）、构建层（演进/
索引重建）、检索层（召回）、控制层（授权/策略变更）。各层注入同一个
AuditLogger 实例记录同一结构的 :class:`~common.type_def.AuditEvent`，
审计链才完整可回溯。持久化由 ``src/storage`` 的审计后端承担；查询与
回溯由控制层治理接口提供，本接口只管「记」。

注意：它不是模型能力插件（无状态计算），所以不继承 Plugin、不进
PluginType，单独成一类横切组件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..factory.factory import Factory
from ..type_def import AuditEvent


@dataclass(frozen=True)
class AuditIntegrityResult:
    """审计完整性校验结果（审计 PR③ P2-2：结构化状态，非裸 list[int]）。

    - ``status``：``unsupported``（后端无完整性保护）/ ``clean``（已验证无篡改）/
      ``tampered``（检出篡改）。
    - ``tampered_indices``：篡改行索引（按写入序，去重，上限采样）。
    - ``checked``：是否实际执行了校验（unsupported 时 False）。
    - ``tampered_count``：实际篡改总数（审计 P3）。
    - ``samples_truncated``：采样是否被截断（实际篡改数 > indices 长度，审计 P3）。

    调用方据此区分「已验证且干净」与「根本没有完整性保护」，不再以空列表表示成功。
    采样上限防止大量篡改耗尽内存，``tampered_count`` 和 ``samples_truncated`` 让调用方
    了解真实损坏规模（100 个索引可能对应 100 个或数百万个篡改）。

    **位置参数兼容性（审计 P2-2）**：``checked`` 保持在第三位（原位置），新字段追加在后，
    保证 ``AuditIntegrityResult("clean", [], True)`` 等旧式调用仍然有效。
    """

    status: str  # unsupported / clean / tampered
    tampered_indices: list[int] = field(default_factory=list)
    checked: bool = False
    tampered_count: int = 0
    samples_truncated: bool = False


class AuditProducer(Factory):
    """AuditLogger 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``audit_impl`` 下以 ``@AuditProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "audit"


class AuditLogger(ABC):
    @abstractmethod
    def record(self, event: AuditEvent) -> None:
        """记录一条审计事件（应尽量异步/低开销，不阻塞业务链路）。"""

    @abstractmethod
    def query(
        self, filters: dict[str, str], limit: int = 100, *, offset: int = 0
    ) -> list[AuditEvent]:
        """按条件检索审计事件；查询入口由治理层调用，具体过滤由后端实现。

        ``offset`` 跳过前 N 条，供分页/流式遍历（审计 PR③ P2-1 verify 流式）。
        """

    def tail(self, limit: int = 1) -> list[AuditEvent]:
        """返回最近 ``limit`` 条事件（按写入序，最新的在后）。

        默认实现走 :meth:`query` 全量取最后--O(n)。持久化后端应 override 成
        ``ORDER BY seq DESC LIMIT ?``（O(1)），供链式 HMAC 恢复链头等场景用，
        避免每次启动全表读取（审计 PR③ P2-1）。
        """
        events = self.query({}, limit=10**9)
        return events[-limit:] if limit else events

    def record_chained(self, event: AuditEvent, expected_head: str) -> str:
        """链式追加：验证期望链头、写入事件、返回新链头。

        持久化后端应 override 成事务 CAS（``BEGIN IMMEDIATE`` + chain-head 表，
        审计 PR③ P1-1），保证多实例/多连接写同一库时链不分叉。默认实现无事务
        原子性（仅 :meth:`record` + :meth:`tail`），适用于单实例内存后端--
        :class:`HmacAuditLogger` 的实例锁在此覆盖单实例并发。
        """
        self.record(event)
        return event.detail.get("_hmac", "")

    def get_chain_head(self) -> str:
        """当前链头 HMAC（O(1)，供 CAS 重试与启动恢复）。默认走 :meth:`tail`。"""
        last = self.tail(1)
        return last[-1].detail.get("_hmac", "") if last else ""

    def get_chain_state(self) -> tuple[str, int, int, str, int]:
        """稳定快照：返回 ``(head_hmac, head_last_seq, last_event_seq, last_event_hmac,
        schema_version)``。

        默认实现走 :meth:`get_chain_head` + :meth:`tail`（两次读取，非原子）。持久化后端
        应 override 成同一 SQL 事务内读取（审计 P1），避免并发追加期间不一致。
        默认 schema_version=2（内存后端无迁移概念）。
        """
        head = self.get_chain_head()
        last = self.tail(1)
        last_hmac = last[-1].detail.get("_hmac", "") if last else ""
        return (head, 1, 1, last_hmac, 2)

    def iter_chain(self, after_seq: int = 0, limit: int = 1000) -> list[tuple[int, AuditEvent]]:
        """从 ``after_seq`` 之后取 ``limit`` 条 ``(seq, event)``（keyset 分页，审计 P2-1）。

        默认走 :meth:`query` offset。返回伪 seq ``after_seq+i+1``（1-based，
        不重复末行，审计 P1-1）。
        持久化后端应 override 成 ``WHERE seq > ? ORDER BY seq LIMIT ?``（O(limit)），
        避免 OFFSET 二次复杂度。返回 seq 供调用方翻页（keyset 的下一页 after_seq）。
        """
        page = self.query({}, limit=limit, offset=after_seq)
        return [(after_seq + i + 1, e) for i, e in enumerate(page)]

    def verify_integrity(self) -> AuditIntegrityResult:
        """校验审计链完整性，返回结构化结果（审计 P2-2）。

        默认返回 ``status="unsupported"``（普通后端无完整性保护，无从校验）。
        完整性保护装饰器（如 :class:`~security.audit_hmac.HmacAuditLogger`）override
        成实际校验。调用方据此区分「已验证且干净」与「无完整性保护」，不再以空列表
        表示成功（fail-open 语义，审计 P2-2）。
        """
        return AuditIntegrityResult(status="unsupported", checked=False)
