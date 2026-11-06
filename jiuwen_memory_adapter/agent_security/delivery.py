"""最终用户/agent 消息交付门禁；与 LLM 请求、工具执行使用同一审批协议。"""

import asyncio
import inspect
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from ._validation import has_exact_type
from .config import AgentSecurityConfig
from .execution import _freeze
from .frameworks import load_runtime_driver
from .hooks import Hook
from .runtime import Actor, Operation, SecurityDenied, encode_payload


@dataclass(frozen=True)
class DeliveryContext:
    actor: Actor
    operation_id: str
    target: str
    action: str
    recipient: Actor | None = None

    def __post_init__(self):
        if not isinstance(self.actor, Actor) or any(
            not has_exact_type(x, str) or not x or len(x) > 256
            for x in (self.operation_id, self.target, self.action)
        ):
            raise SecurityDenied("交付缺少可信关联")
        if self.recipient is not None:
            if (
                not isinstance(self.recipient, Actor)
                or self.recipient == self.actor
                or self.recipient.tenant_id != self.actor.tenant_id
            ):
                raise SecurityDenied("接收 agent 身份或租户无效")


class DeliveryAdapter:
    def __init__(
        self,
        framework,
        guard,
        *,
        sources,
        context_for,
        present,
        authorize_transfer=None,
        audit=None,
        driver=None,
        capacity=1024,
    ):
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("交付容量必须为正整数")
        self.driver = (
            driver if driver is not None else load_runtime_driver({"framework": framework})
        )
        self.framework, self.guard, self.sources = framework, guard, sources
        self.context_for, self.present = context_for, present
        self.authorize_transfer, self.audit = authorize_transfer, audit
        self.capacity, self._entries, self._pending = capacity, set(), set()
        self._lock, self._closed = threading.RLock(), False

    def resolve_context(self, host, hook):
        context = self.context_for(host, hook)
        if not isinstance(context, DeliveryContext):
            raise SecurityDenied("交付上下文未认证")
        if (hook == Hook.BEFORE_AGENT_MESSAGE) != (context.recipient is not None):
            raise SecurityDenied("接收方类型与交付边界不匹配")
        return context

    def _prepare(self, host, hook, payload):
        hook = Hook(hook)
        if hook not in {Hook.BEFORE_AGENT_RESPONSE, Hook.BEFORE_AGENT_MESSAGE}:
            raise SecurityDenied("交付门禁仅接受用户响应或 agent 消息")
        context = self.resolve_context(host, hook)
        snapshot = self.sources.snapshot(context.actor)
        if hook == Hook.BEFORE_AGENT_MESSAGE:
            if (
                self.authorize_transfer is None
                or self.authorize_transfer(context.actor, context.recipient, snapshot) is not True
            ):
                raise SecurityDenied("agent 消息移交未获授权")
            self.sources.snapshot(context.recipient)
        key = (context.actor, hook, context.operation_id)
        with self._lock:
            if self._closed or key in self._entries or len(self._entries) >= self.capacity:
                raise SecurityDenied("交付已结束、重复或容量已满")
            self._entries.add(key)
        operation = Operation.create(
            actor=context.actor,
            operation_id=context.operation_id,
            hook=hook,
            target=context.target,
            action=context.action,
            payload=json.loads(encode_payload(payload)),
            source_level=snapshot.level,
            source_version=snapshot.version,
        )
        request = self.guard.prepare(operation)
        if request is not None:
            with self._lock:
                self._pending.add(request.approval_id)
        return context, operation, request, self.guard.revision

    def _audit(self, operation, outcome):
        if self.audit is not None:
            self.audit.emit(
                actor=operation.actor,
                hook=operation.hook,
                operation_id=operation.operation_id,
                target=operation.target,
                level=operation.source_level,
                outcome=outcome,
            )

    def _check(self, host, context, operation, revision):
        state = self.sources.snapshot(operation.actor)
        if (
            self._closed
            or self.resolve_context(host, operation.hook) != context
            or state.level != operation.source_level
        ):
            raise SecurityDenied("交付身份、来源、目标或策略已改变")
        elif (
            state.version != operation.source_version
            or self.guard.revision != revision
            or self.guard.authorize(operation) is not True
        ):
            raise SecurityDenied("交付身份、来源、目标或策略已改变")
        elif operation.target not in self.guard.policy.allowed_targets:
            raise SecurityDenied("交付身份、来源、目标或策略已改变")

    def _inherit(self, context):
        if context.recipient is not None:
            self.sources.inherit(
                context.actor, context.recipient, authorize_transfer=self.authorize_transfer
            )

    def _done(self, request):
        if request is not None:
            self.guard.broker.cancel(request.approval_id)
            with self._lock:
                self._pending.discard(request.approval_id)

    async def asend(self, *, host, hook, payload, send):
        request, operation, started = None, None, False
        try:
            context, operation, request, revision = self._prepare(host, hook, payload)
            self._audit(operation, "prepared")
            if request is not None:
                result = self.present(request.public(), actor=operation.actor)
                if inspect.isawaitable(result):
                    await result
                await self.guard.broker.wait(request.approval_id)
                self._check(host, context, operation, revision)
                selected = self.guard.claim(request, operation)
            else:
                selected = operation.payload
            async with self.sources.alease(context.actor, operation.source_version):
                self._check(host, context, operation, revision)
                await asyncio.to_thread(self._inherit, context)
                self._audit(operation, "started")
                started = True
                result = send(_freeze(selected), context)
                if inspect.isawaitable(result):
                    result = await result
            self._audit(operation, "completed")
            return result
        except BaseException:
            if operation is not None:
                self._audit(operation, "unknown" if started else "aborted")
            raise
        finally:
            self._done(request)

    def create_binding(self):
        return self.driver.create_delivery_binding(self)

    async def aapprove_native(self, public, *, host, hook):
        context = self.resolve_context(host, hook)
        original = self.guard.broker.public_request(public["approval_id"], actor=context.actor)
        if original != public:
            raise SecurityDenied("原生中断引用与可信审批不一致")
        result = self.present(public, actor=context.actor)
        if inspect.isawaitable(result):
            await result
        await self.guard.broker.wait(public["approval_id"])
        return self.guard.broker.decision(public["approval_id"], reviewer=context.actor)

    def close(self):
        with self._lock:
            self._closed = True
            for key in self._pending:
                self.guard.broker.cancel(key)
            self._pending.clear()
            self._entries.clear()


def create_delivery_adapter(
    config: AgentSecurityConfig | Mapping[str, object],
    guard,
    *,
    driver=None,
    runtime_registry=None,
    **ports,
):
    if not isinstance(config, AgentSecurityConfig):
        config = AgentSecurityConfig.from_mapping(config)
    selected = (
        driver if driver is not None else load_runtime_driver(config, registry=runtime_registry)
    )
    if selected.framework != config.framework:
        raise SecurityDenied("运行时驱动与框架配置不匹配")
    return DeliveryAdapter(config.framework, guard, driver=selected, **ports)
