"""最小实现：:class:`~common.audit.base.AuditLogger` 的纯内存审计后端。

把所有审计事件留在内存列表里，供控制层治理 audit 按条件过滤查询。
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import List

from common.audit.base import AuditLogger, AuditProducer
from common.errors import ConflictError
from common.type_def import AuditEvent


class InMemoryAuditLogger(AuditLogger):
    """内存审计后端：记录全部事件，供治理 audit 按条件过滤查询。"""

    def __init__(self) -> None:
        self.events: List[AuditEvent] = []
        self._lock = RLock()  # 保护并发访问（审计 P1-1）

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self.events.append(event)

    def query(
        self, filters: dict[str, str], limit: int = 100, *, offset: int = 0
    ) -> list[AuditEvent]:
        with self._lock:
            out: list[AuditEvent] = []
            skipped = 0
            for event in self.events:
                if not _matches(event, filters):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                out.append(event)
                if len(out) >= limit:
                    break
            return out

    def tail(self, limit: int = 1) -> list[AuditEvent]:
        # list 切片 O(k)，避免默认实现的全量 query
        with self._lock:
            return self.events[-limit:] if limit else list(self.events)

    def record_chained(self, event: AuditEvent, expected_head: str) -> str:
        """单实例线程安全链式追加（审计 P1-1 capability）。

        InMemoryAuditLogger 用锁保护 CAS 检查：expected_head 不匹配时抛
        ConflictError。单实例多线程场景安全；多实例场景仍需持久化后端。
        """
        with self._lock:
            current_head = self.events[-1].detail.get("_hmac", "") if self.events else ""
            if current_head != expected_head:
                raise ConflictError(
                    f"audit chain CAS conflict: expected {expected_head}, got {current_head}"
                )
            self.events.append(event)
            return event.detail.get("_hmac", "")

    def supports_chain_cas(self) -> bool:
        """InMemory 后端支持单实例线程级 CAS（审计 P1-1）。"""
        return True

    def get_chain_state(self) -> tuple[str, int, int, str, int]:
        """内存后端链状态（单进程，head 即最后事件 _hmac）。"""
        with self._lock:
            if not self.events:
                return ("", 0, 0, "", 2)
            last = self.events[-1]
            hmac_val = last.detail.get("_hmac", "")
            n = len(self.events)
            return (hmac_val, n, n, hmac_val, 2)


# -- 注册到 AuditProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@AuditProducer.register("in_memory")
def _build(config):
    return InMemoryAuditLogger()


def _matches(event: AuditEvent, filters: dict[str, str]) -> bool:
    for field in ("action", "layer", "decision", "target_id"):
        if filters.get(field) and getattr(event, field) != filters[field]:
            return False

    actor_filters = {
        "actor_org": event.actor.org,
        "actor_space": event.actor.space,
        "actor_user": event.actor.user,
        "actor_agent": event.actor.agent,
        "actor_session": event.actor.session,
    }
    for field, value in actor_filters.items():
        if filters.get(field) and value != filters[field]:
            return False

    target_filters = {
        "target_org": event.target.org,
        "target_space": event.target.space,
        "target_user": event.target.user,
        "target_agent": event.target.agent,
        "target_session": event.target.session,
    }
    for field, value in target_filters.items():
        if filters.get(field) and value != filters[field]:
            return False

    occurred_at = _as_utc(event.occurred_at)
    after = _parse_datetime(filters.get("occurred_after"))
    if after is not None and (occurred_at is None or occurred_at < after):
        return False
    before = _parse_datetime(filters.get("occurred_before"))
    if before is not None and (occurred_at is None or occurred_at > before):
        return False
    return True


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return _as_utc(datetime.fromisoformat(raw))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
