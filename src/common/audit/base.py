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

from ..type_def import AuditEvent


class AuditLogger(ABC):
    @abstractmethod
    def record(self, event: AuditEvent) -> None:
        """记录一条审计事件（应尽量异步/低开销，不阻塞业务链路）。"""
