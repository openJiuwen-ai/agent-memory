"""有界、无正文审计；事件不能接收任意异常或业务字段。"""

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from ._validation import has_exact_type
from .hooks import Hook
from .runtime import Actor, SecurityDenied, encode_payload


@dataclass(frozen=True)
class SecurityAuditEvent:
    timestamp: float
    hook: str
    outcome: str
    actor_ref: str
    operation_ref: str
    target_ref: str
    level: str


class SecurityAuditLog:
    """sink 只收到固定元数据；满额拒绝新事件，宿主 drain 后再继续。"""

    def __init__(self, *, capacity=4096, sink=None):
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("审计容量必须为正整数")
        self.capacity, self.sink = capacity, sink
        self._secret, self._events = secrets.token_bytes(32), deque()
        self._lock = threading.RLock()
        self._closed = False

    def _ref(self, value):
        return hmac.new(self._secret, encode_payload(value).encode(), hashlib.sha256).hexdigest()

    def emit(self, *, actor, hook, operation_id, target, level, outcome):
        if (
            not isinstance(actor, Actor)
            or level not in {"S1", "S2", "S3"}
            or outcome
            not in {
                "prepared",
                "approved",
                "aborted",
                "started",
                "completed",
                "failed",
                "cancelled",
                "redacted",
                "released",
                "unknown",
            }
        ):
            raise SecurityDenied("无效的安全审计事件")
        event = SecurityAuditEvent(
            time.time(),
            str(Hook(hook)),
            outcome,
            self._ref(vars(actor)),
            self._ref(operation_id),
            self._ref(target),
            level,
        )
        with self._lock:
            if self._closed or len(self._events) >= self.capacity:
                raise SecurityDenied("安全审计不可用或容量已满")
            if self.sink is not None:
                try:
                    self.sink(event)
                except Exception:
                    raise SecurityDenied("安全审计写入失败") from None
            self._events.append(event)
        return event

    def actor_ref(self, actor):
        if not isinstance(actor, Actor):
            raise SecurityDenied("审计主体未知")
        return self._ref(vars(actor))

    def drain(self, *, actor=None):
        with self._lock:
            reference = self.actor_ref(actor) if actor is not None else None
            events = tuple(
                asdict(event)
                for event in self._events
                if reference is None or event.actor_ref == reference
            )
            self._events = deque(
                event
                for event in self._events
                if reference is not None and event.actor_ref != reference
            )
            return events

    def close(self):
        with self._lock:
            self._closed = True
            self._events.clear()
