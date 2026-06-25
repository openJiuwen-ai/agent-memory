"""最小实现：:class:`~common.audit.base.AuditLogger` 的纯内存审计后端。

把所有审计事件留在内存列表里，供控制层治理 audit 按条件过滤查询。
"""

from __future__ import annotations

from typing import List

from common.audit.base import AuditLogger, AuditProducer
from common.type_def import AuditEvent


class InMemoryAuditLogger(AuditLogger):
    """内存审计后端：记录全部事件，供治理 audit 按条件过滤查询。"""

    def __init__(self) -> None:
        self.events: List[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


# -- 注册到 AuditProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@AuditProducer.register("in_memory")
def _build(config):
    return InMemoryAuditLogger()
