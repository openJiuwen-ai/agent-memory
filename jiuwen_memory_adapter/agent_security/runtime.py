"""敏感外发准备、三选项审批与单次领取；不承担宿主身份认证。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ._validation import has_exact_type
from .hooks import Hook
from .inspection import TextInspector


class SecurityDenied(RuntimeError):
    """安全失败；异常文本不得包含业务载荷。"""


class ApprovalChoice(StrEnum):
    CONTINUE_ORIGINAL = "CONTINUE_ORIGINAL"
    ABORT = "ABORT"
    SEND_REDACTED = "SEND_REDACTED"


class ApprovalState(StrEnum):
    WAITING = "WAITING_USER"
    DECIDED = "DECIDED"
    CONSUMED = "CONSUMED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


def encode_payload(value: Any) -> str:
    """有界 JSON 快照；拒绝自定义对象、非有限数字和隐式类型转换。"""

    def validate(item, depth=0):
        if depth > 24:
            raise SecurityDenied("载荷嵌套过深")
        if has_exact_type(item, dict):
            for key, child in item.items():
                if not has_exact_type(key, str):
                    raise SecurityDenied("载荷键必须是字符串")
                validate(child, depth + 1)
        elif has_exact_type(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and not has_exact_type(item, (str, int, float, bool)):
            raise SecurityDenied("不支持的载荷类型")

    try:
        validate(value)
        result = json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        if len(result.encode("utf-8")) > 262144:
            raise SecurityDenied("载荷过大")
        return result
    except (ValueError, TypeError, RecursionError):
        raise SecurityDenied("载荷不是受支持的 JSON") from None


@dataclass(frozen=True)
class Actor:
    """仅由宿主认证和 session 解析器构造，不从模型/审批请求体读取。"""

    tenant_id: str
    user_id: str
    session_id: str
    agent_id: str
    incarnation: str

    def __post_init__(self):
        if any(not has_exact_type(v, str) or not v or len(v) > 256 for v in vars(self).values()):
            raise SecurityDenied("缺少可信宿主身份")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    actor: Actor
    hook: Hook
    target: str
    action: str
    payload_json: str = field(repr=False)
    source_level: str
    source_version: str

    @classmethod
    def create(cls, *, payload, **context):
        return cls(payload_json=encode_payload(payload), **context)

    def __post_init__(self):
        if not isinstance(self.actor, Actor):
            raise SecurityDenied("缺少可信宿主身份")
        if any(
            not has_exact_type(v, str) or not v or len(v) > 512
            for v in (self.operation_id, self.target, self.action, self.source_version)
        ):
            raise SecurityDenied("缺少操作关联或目标")
        object.__setattr__(self, "hook", Hook(self.hook))
        if self.hook not in {
            Hook.BEFORE_LLM_CALL,
            Hook.BEFORE_TOOL_CALL,
            Hook.BEFORE_AGENT_RESPONSE,
            Hook.BEFORE_AGENT_MESSAGE,
        }:
            raise SecurityDenied("此审批执行器只接受发送前边界")
        if self.source_level not in {"S1", "S2", "S3"}:
            raise SecurityDenied("未知来源等级")
        try:
            canonical = encode_payload(json.loads(self.payload_json))
        except (ValueError, TypeError):
            raise SecurityDenied("载荷快照无效") from None
        object.__setattr__(self, "payload_json", canonical)

    @property
    def payload(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class PayloadInspection:
    level: str
    redacted_json: str | None = field(repr=False)
    rules: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


class PayloadInspector:
    """只消费公开检测端口；默认不改写目标/工具名/强类型参数。"""

    def __init__(self, inspector: TextInspector, editable_fields: frozenset[str]):
        self.text = inspector
        self.editable_fields = frozenset(editable_fields)

    @property
    def version(self):
        return hashlib.sha256(
            encode_payload([self.text.version, sorted(self.editable_fields)]).encode()
        ).hexdigest()

    def inspect(self, payload) -> PayloadInspection:
        payload = json.loads(encode_payload(payload))
        levels, rules, categories = [1], set(), set()
        possible, leaves, deadline = True, 0, time.monotonic() + 3.0

        def scan(text, label=""):
            nonlocal leaves
            leaves += 1
            if leaves > 2048 or time.monotonic() > deadline:
                raise SecurityDenied("载荷检测预算耗尽")
            result = self.text.inspect(text, label=label)
            if result.level_id not in {"S1", "S2", "S3"}:
                raise SecurityDenied("检测器返回未知等级")
            levels.append(int(result.level_id[1]))
            rules.update(result.matched_rules)
            categories.update(result.categories)
            return result.redacted

        def walk(value, root="", label=""):
            nonlocal possible
            if has_exact_type(value, dict):
                result = {}
                for key, child in value.items():
                    if scan(key) != key:
                        possible = False
                    result[key] = walk(child, root or key, key)
                return result
            if has_exact_type(value, list):
                return [walk(v, root, label) for v in value]
            if has_exact_type(value, (str, int, float)):
                original = str(value)
                safe = scan(original, label)
                if safe != original:
                    if root not in self.editable_fields or not has_exact_type(value, str):
                        possible = False
                    return safe
            return value

        redacted = walk(payload)
        return PayloadInspection(
            f"S{max(levels)}",
            encode_payload(redacted) if possible else None,
            tuple(sorted(rules)[:64]),
            tuple(sorted(categories)[:64]),
        )


@dataclass(frozen=True)
class DefaultEgressPolicy:
    """PR #406 三级含义；外发许可独立于 write/reveal。目标由宿主显式登记。"""

    allowed_targets: frozenset[str]
    version: str = "egress-1"

    def __post_init__(self):
        object.__setattr__(self, "allowed_targets", frozenset(self.allowed_targets))

    def choices(self, operation: Operation, inspection: PayloadInspection):
        if operation.target not in self.allowed_targets:
            raise SecurityDenied("目标未获外发授权")
        choices = [ApprovalChoice.ABORT]
        if inspection.level != "S3":
            choices.insert(0, ApprovalChoice.CONTINUE_ORIGINAL)
        if inspection.redacted_json is not None:
            choices.append(ApprovalChoice.SEND_REDACTED)
        if choices == [ApprovalChoice.ABORT]:
            raise SecurityDenied("不存在可以发送的候选")
        return tuple(choices)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    version: str
    operation_id: str
    target: str
    action: str
    level: str
    choices: tuple[ApprovalChoice, ...]
    expires_at: float
    preview: Any = field(repr=False)

    def public(self) -> dict:
        labels = {
            ApprovalChoice.CONTINUE_ORIGINAL: "1. 继续外发（仅本次）",
            ApprovalChoice.ABORT: "2. 中止外发",
            ApprovalChoice.SEND_REDACTED: "3. 脱敏外发",
        }
        return {
            "schema_version": 1,
            "approval_id": self.approval_id,
            "version": self.version,
            "operation_id": self.operation_id,
            "target": self.target,
            "action": self.action,
            "level": self.level,
            "expires_at": self.expires_at,
            "message": "本次外发包含敏感信息，请选择如何处理。",
            "options": [{"value": str(c), "label": labels[c]} for c in self.choices],
            "preview": json.loads(encode_payload(self.preview)),
        }


@dataclass(repr=False)
class _Entry:
    operation: Operation
    request: ApprovalRequest
    fingerprint: str
    redacted_json: str | None
    state: ApprovalState = ApprovalState.WAITING
    decision: ApprovalChoice | None = None
    waiters: list = field(default_factory=list)


class ApprovalBroker:
    """有界进程内请求库。重启不恢复原调用，过期/拒绝墓碑保留到实例关闭。"""

    def __init__(self, *, ttl=120.0, capacity=1024, clock=time.time, audit=None):
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 600:
            raise ValueError("审批期限必须在 (0, 600] 秒")
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("审批容量必须为正整数")
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.audit = audit
        self._secret = secrets.token_bytes(32)
        self._entries: dict[str, _Entry] = {}
        self._operations: dict[tuple, str] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _fingerprint(self, operation, revision):
        data = encode_payload(
            [
                vars(operation.actor),
                str(operation.hook),
                operation.target,
                operation.action,
                operation.payload,
                operation.source_level,
                operation.source_version,
                revision,
            ]
        )
        return hmac.new(self._secret, data.encode(), hashlib.sha256).hexdigest()

    def prepare(self, operation, inspection, choices, revision):
        fingerprint = self._fingerprint(operation, revision)
        key = (operation.actor, operation.hook, operation.operation_id)
        with self._lock:
            if self._closed:
                raise SecurityDenied("审批服务已关闭")
            if key in self._operations:
                entry = self._entry(self._operations[key])
                if entry.fingerprint != fingerprint:
                    self._finish(entry, ApprovalState.INVALIDATED)
                    raise SecurityDenied("待审批操作已改变")
                if entry.state not in {ApprovalState.WAITING, ApprovalState.DECIDED}:
                    raise SecurityDenied("该操作已关闭或已经领取")
                return entry.request
            if len(self._entries) >= self.capacity:
                raise SecurityDenied("审批容量已满，请轮换已结束的会话实例")
            request = ApprovalRequest(
                secrets.token_urlsafe(24),
                secrets.token_urlsafe(16),
                operation.operation_id,
                operation.target,
                operation.action,
                f"S{max(int(operation.source_level[1]), int(inspection.level[1]))}",
                choices,
                self.clock() + self.ttl,
                json.loads(inspection.redacted_json)
                if inspection.redacted_json is not None
                else None,
            )
            self._entries[request.approval_id] = _Entry(
                operation, request, fingerprint, inspection.redacted_json
            )
            self._operations[key] = request.approval_id
            return request

    def _entry(self, approval_id):
        entry = self._entries.get(approval_id)
        if entry is None:
            raise SecurityDenied("审批请求不存在")
        if (
            entry.state in {ApprovalState.WAITING, ApprovalState.DECIDED}
            and self.clock() >= entry.request.expires_at
        ):
            self._finish(entry, ApprovalState.EXPIRED)
        return entry

    @staticmethod
    def _finish(entry, state):
        entry.state = state
        waiters, entry.waiters = entry.waiters, []
        for loop, future in waiters:

            def wake(future=future):
                if not future.done():
                    future.set_result(None)

            if not loop.is_closed():
                loop.call_soon_threadsafe(wake)

    def submit(self, approval_id, version, choice, *, reviewer: Actor):
        try:
            choice = ApprovalChoice(choice)
        except (TypeError, ValueError):
            raise SecurityDenied("无效的审批选项") from None
        with self._lock:
            entry = self._entry(approval_id)
            if reviewer != entry.operation.actor or version != entry.request.version:
                raise SecurityDenied("审批身份或请求版本不匹配")
            if entry.state == ApprovalState.DECIDED and entry.decision == choice:
                return  # 回调重投可确认，但领取只允许一次。
            if entry.state != ApprovalState.WAITING or choice not in entry.request.choices:
                raise SecurityDenied("审批已关闭或选项不允许")
            if self.audit is not None:
                operation = entry.operation
                self.audit.emit(
                    actor=operation.actor,
                    hook=operation.hook,
                    operation_id=operation.operation_id,
                    target=operation.target,
                    level=entry.request.level,
                    outcome="aborted" if choice == ApprovalChoice.ABORT else "approved",
                )
            entry.decision = choice
            self._finish(
                entry,
                ApprovalState.ABORTED if choice == ApprovalChoice.ABORT else ApprovalState.DECIDED,
            )

    def claim(self, approval_id, operation, revision):
        with self._lock:
            entry = self._entry(approval_id)
            if entry.fingerprint != self._fingerprint(operation, revision):
                self._finish(entry, ApprovalState.INVALIDATED)
                raise SecurityDenied("审批快照或策略已改变")
            if entry.state != ApprovalState.DECIDED:
                raise SecurityDenied("审批未批准、已关闭或已领取")
            selected = (
                entry.redacted_json
                if entry.decision == ApprovalChoice.SEND_REDACTED
                else entry.operation.payload_json
            )
            if selected is None:
                raise SecurityDenied("脱敏候选不可用")
            self._finish(entry, ApprovalState.CONSUMED)
            return json.loads(selected)

    def cancel(self, approval_id):
        with self._lock:
            if self._closed and approval_id not in self._entries:
                return
            entry = self._entry(approval_id)
            if entry.state in {ApprovalState.WAITING, ApprovalState.DECIDED}:
                self._finish(entry, ApprovalState.CANCELLED)

    def state(self, approval_id):
        with self._lock:
            return self._entry(approval_id).state

    def decision(self, approval_id, *, reviewer):
        """原生 continuation 控制器读取已认证决定；此处不领取或执行原操作。"""
        with self._lock:
            entry = self._entry(approval_id)
            if (
                entry.operation.actor != reviewer
                or entry.state
                not in {
                    ApprovalState.DECIDED,
                    ApprovalState.ABORTED,
                }
                or entry.decision is None
            ):
                raise SecurityDenied("审批决定不存在、过期或主体不符")
            return entry.decision

    def public_request(self, approval_id, *, actor):
        with self._lock:
            entry = self._entry(approval_id)
            if entry.operation.actor != actor:
                raise SecurityDenied("审批请求主体不符")
            return entry.request.public()

    async def wait(self, approval_id):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            entry = self._entry(approval_id)
            if entry.state == ApprovalState.WAITING:
                entry.waiters.append((loop, future))
                remaining = max(0, entry.request.expires_at - self.clock())
            else:
                return
        try:
            await asyncio.wait_for(future, remaining)
        except TimeoutError:
            with self._lock:
                if entry.state == ApprovalState.WAITING:
                    self._finish(entry, ApprovalState.EXPIRED)
        except asyncio.CancelledError:
            self.cancel(approval_id)
            raise
        finally:
            with self._lock:
                if (loop, future) in entry.waiters:
                    entry.waiters.remove((loop, future))

    def close(self):
        with self._lock:
            self._closed = True
            for entry in self._entries.values():
                if entry.state in {ApprovalState.WAITING, ApprovalState.DECIDED}:
                    self._finish(entry, ApprovalState.CANCELLED)
            self._entries.clear()
            self._operations.clear()


class EgressGuard:
    def __init__(
        self,
        inspector: PayloadInspector,
        policy: DefaultEgressPolicy,
        broker: ApprovalBroker,
        *,
        authorize: Callable[[Operation], bool],
        audit=None,
    ):
        self.inspector, self.policy, self.broker, self.authorize = (
            inspector,
            policy,
            broker,
            authorize,
        )
        self.audit = audit

    @property
    def revision(self):
        return hashlib.sha256(
            encode_payload(
                [self.policy.version, sorted(self.policy.allowed_targets), self.inspector.version]
            ).encode()
        ).hexdigest()

    def prepare(self, operation: Operation) -> ApprovalRequest | None:
        if (
            self.authorize(operation) is not True
            or operation.target not in self.policy.allowed_targets
        ):
            raise SecurityDenied("当前主体无权执行该操作")
        inspection = self.inspector.inspect(operation.payload)
        choices = self.policy.choices(operation, inspection)
        if self.audit is not None:
            self.audit.emit(
                actor=operation.actor,
                hook=operation.hook,
                operation_id=operation.operation_id,
                target=operation.target,
                level=max(operation.source_level, inspection.level),
                outcome="prepared",
            )
        if (
            inspection.level == operation.source_level == "S1"
            and inspection.redacted_json == operation.payload_json
        ):
            return None
        # 候选必须再次检查；脱敏不降低来源标签，也不赋予新的目的地权限。
        if inspection.redacted_json is not None:
            check = self.inspector.inspect(json.loads(inspection.redacted_json))
            if check.level != "S1" or check.redacted_json != inspection.redacted_json:
                choices = tuple(c for c in choices if c != ApprovalChoice.SEND_REDACTED)
        if choices == (ApprovalChoice.ABORT,):
            raise SecurityDenied("候选复检失败")
        return self.broker.prepare(operation, inspection, choices, self.revision)

    def claim(self, request: ApprovalRequest, operation: Operation):
        if (
            self.authorize(operation) is not True
            or operation.target not in self.policy.allowed_targets
        ):
            self.broker.cancel(request.approval_id)
            raise SecurityDenied("审批后权限已撤销")
        return self.broker.claim(request.approval_id, operation, self.revision)

    async def gate(self, operation, *, present, refresh):
        """present 在原会话展示控制消息；refresh 从宿主重新取得当前操作事实。"""
        request = self.prepare(operation)
        if request is None:
            return operation.payload
        try:
            await present(request.public())
            await self.broker.wait(request.approval_id)
            return self.claim(request, refresh())
        finally:
            self.broker.cancel(request.approval_id)
