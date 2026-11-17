"""最终 HTTP 请求门禁：实际 URL/模型决定目标，每次重试独立检测和审批。"""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from ._validation import has_exact_type
from .config import AgentSecurityConfig
from .frameworks import load_runtime_driver
from .hooks import Hook
from .runtime import Actor, Operation, SecurityDenied, encode_payload


def _check_url_text(value: str):
    if not has_exact_type(value, str) or not value or len(value) > 2048:
        raise SecurityDenied("模型请求目标缺失或过长")
    if any(ord(c) <= 32 or ord(c) > 126 for c in value) or "\\" in value:
        raise SecurityDenied("模型请求目标不是规范 URL")


def normalize_llm_endpoint(value: str) -> str:
    """完整请求 URL，拒绝凭据/query/fragment；不从 provider 名称猜测可信度。"""
    _check_url_text(value)
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
        ):
            raise ValueError
        elif parts.password is not None or parts.query or parts.fragment:
            raise ValueError
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port == 0:
            raise ValueError
        if port and port != {"http": 80, "https": 443}[parts.scheme]:
            host += f":{port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", "", ""))
    except ValueError:
        raise SecurityDenied("模型请求目标不受支持") from None


def chat_endpoint(api_base: str) -> str:
    """按 URL 组件追加 Chat 路径，保留 query/fragment 供规范化门禁拒绝。"""
    _check_url_text(api_base)
    parts = urlsplit(api_base)
    endpoint = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"{parts.path.rstrip('/')}/chat/completions",
            parts.query,
            parts.fragment,
        )
    )
    return normalize_llm_endpoint(endpoint)


@dataclass(frozen=True)
class LlmTarget:
    target_id: str
    provider: str
    model: str
    endpoint: str
    protocol: str = "openai-chat"

    def __post_init__(self):
        if (
            any(
                not has_exact_type(x, str) or not x or len(x) > 256
                for x in (self.target_id, self.provider, self.model)
            )
            or self.protocol != "openai-chat"
        ):
            raise SecurityDenied("模型目标策略无效或协议未覆盖")
        object.__setattr__(self, "endpoint", normalize_llm_endpoint(self.endpoint))


@dataclass(frozen=True)
class LlmCallContext:
    actor: Actor
    call_id: str

    def __post_init__(self):
        if not isinstance(self.actor, Actor) or not has_exact_type(self.call_id, str):
            raise SecurityDenied("缺少可信模型调用关联")
        if not self.call_id or len(self.call_id) > 256:
            raise SecurityDenied("缺少可信模型调用关联")


@dataclass
class _Call:
    context: LlmCallContext
    attempts: int = 0
    stopped: bool = False


def _chat_payload(body):
    if not has_exact_type(body, dict) or not has_exact_type(body.get("model"), str):
        raise SecurityDenied("仅支持含明确模型的 JSON Chat 请求")
    messages = body.get("messages")
    if not has_exact_type(messages, list) or not messages:
        raise SecurityDenied("模型请求消息格式不受支持")
    for message in messages:
        if not has_exact_type(message, dict) or not has_exact_type(message.get("role"), str):
            raise SecurityDenied("模型请求消息格式不受支持")
        content = message.get("content")
        if content is not None and not has_exact_type(content, str):
            if not has_exact_type(content, list) or any(
                not has_exact_type(block, dict)
                or block.get("type") != "text"
                or not has_exact_type(block.get("text"), str)
                or set(block) - {"type", "text", "cache_control"}
                for block in content
            ):
                raise SecurityDenied("多模态与附件请求尚未接入")
    if "stream" in body and not has_exact_type(body["stream"], bool):
        raise SecurityDenied("模型流标志无效")
    return json.loads(encode_payload(body))


def validate_llm_candidate(original, selected):
    """只允许消息内容/历史工具参数文字改变，模型、角色、工具身份和参数形状不改。"""
    selected = _chat_payload(selected)
    if (
        set(selected) != set(original)
        or any(original[k] != selected[k] for k in original if k != "messages")
        or len(original["messages"]) != len(selected["messages"])
    ):
        raise SecurityDenied("脱敏候选改变模型请求控制字段")
    for before, after in zip(original["messages"], selected["messages"], strict=True):
        if set(before) != set(after) or any(
            before[k] != after[k] for k in before if k not in {"content", "tool_calls"}
        ):
            raise SecurityDenied("脱敏候选改变消息身份")
        if not has_exact_type(before.get("content"), type(after.get("content"))):
            raise SecurityDenied("脱敏候选改变文本表示")
        if has_exact_type(before.get("content"), list):
            if len(before["content"]) != len(after["content"]):
                raise SecurityDenied("脱敏候选改变文本块数量")
            for a, b in zip(before["content"], after["content"], strict=True):
                if set(a) != set(b) or any(a[k] != b[k] for k in a if k != "text"):
                    raise SecurityDenied("脱敏候选改变文本块控制字段")
        calls, edited = before.get("tool_calls", []), after.get("tool_calls", [])
        if (
            not has_exact_type(calls, list)
            or not has_exact_type(edited, list)
            or len(calls) != len(edited)
        ):
            raise SecurityDenied("历史工具调用格式改变")
        for a, b in zip(calls, edited, strict=True):
            if not has_exact_type(a, dict) or not has_exact_type(b, dict) or set(a) != set(b):
                raise SecurityDenied("历史工具调用格式改变")
            if any(a[k] != b[k] for k in a if k != "function"):
                raise SecurityDenied("历史工具调用身份改变")
            fa, fb = a.get("function"), b.get("function")
            if not has_exact_type(fa, dict) or not has_exact_type(fb, dict) or set(fa) != set(fb):
                raise SecurityDenied("历史工具函数格式改变")
            if any(fa[k] != fb[k] for k in fa if k != "arguments"):
                raise SecurityDenied("历史工具函数身份改变")
    return selected


class LlmAdapter:
    """显式作用域及最终 transport。不会接管未安装的客户端或服务器内部路由。"""

    def __init__(
        self,
        framework,
        guard,
        *,
        targets,
        sources,
        context_for,
        present,
        driver=None,
        capacity=1024,
        max_attempts=32,
        results=None,
        audit=None,
    ):
        if any(not has_exact_type(n, int) or n < 1 for n in (capacity, max_attempts)):
            raise ValueError("模型门禁容量与重试上限必须为正整数")
        self.driver = (
            driver if driver is not None else load_runtime_driver({"framework": framework})
        )
        self.framework, self.guard, self.sources = framework, guard, sources
        self.context_for, self.present = context_for, present
        self.capacity, self.max_attempts = capacity, max_attempts
        self.results, self.audit = results, audit
        self._targets = {}
        for target in targets:
            key = (target.endpoint, target.model)
            if key in self._targets or len(self._targets) >= capacity:
                raise SecurityDenied("模型目标重复、归属不明确或数量超限")
            self._targets[key] = target
        self._frames = ContextVar(f"jiuwen_llm_{id(self)}", default=None)
        self._wire = ContextVar(f"jiuwen_llm_wire_{id(self)}", default=None)
        self._calls = {}
        self._pending = set()
        self._cleanup = []
        self._native_provider = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def targets(self):
        """只读实际模型目标目录，供合作 transport/provider 解析目标。"""
        return MappingProxyType(self._targets)

    def clear_scope(self):
        return self._frames.set(None)

    def restore_scope(self, token):
        self._frames.reset(token)

    def wire_operation(self):
        return self._wire.get()

    def own_provider(self, register, release):
        """在同一容量与生命周期锁内登记合作 provider 及清理回调。"""
        with self._lock:
            if self._closed or len(self._cleanup) >= self.capacity:
                raise SecurityDenied("受保护 provider 已关闭或注册容量已满")
            register()
            self._cleanup.append(release)

    def enter_scope(self, host):
        context = self.context_for(host)
        if not isinstance(context, LlmCallContext):
            raise SecurityDenied("缺少可信模型调用上下文")
        self.sources.snapshot(context.actor)
        with self._lock:
            if self._closed:
                raise SecurityDenied("模型请求门禁已关闭")
            key = (context.actor, context.call_id)
            if key not in self._calls:
                if len(self._calls) >= self.capacity:
                    raise SecurityDenied("模型调用容量已满")
                self._calls[key] = _Call(context)
            call = self._calls[key]
            if call.stopped:
                raise SecurityDenied("本次模型调用已经中止")
            # 重试/fallback 可使用新的原生 request 对象，但共享同一可信逻辑调用 ID。
            return self._frames.set((call, host))

    @contextmanager
    def scope(self, host):
        token = self.enter_scope(host)
        try:
            yield
        finally:
            self._frames.reset(token)

    def current_call(self):
        frame = self._frames.get()
        if self._closed or frame is None:
            raise SecurityDenied("缺少可信模型发送作用域")
        call, host = frame
        if call.stopped or self.context_for(host) != call.context:
            raise SecurityDenied("当前模型主体或调用关联已改变")
        return call

    def _prepare(self, endpoint, body):
        call = self.current_call()
        payload = _chat_payload(body)
        target = self._targets.get((normalize_llm_endpoint(endpoint), payload["model"]))
        if target is None:
            call.stopped = True
            raise SecurityDenied("实际模型目标未登记")
        with self._lock:
            call.attempts += 1
            if call.attempts > self.max_attempts:
                call.stopped = True
                raise SecurityDenied("模型实际请求次数超限")
            attempt = call.attempts
        snapshot = self.sources.snapshot(call.context.actor)
        operation = Operation.create(
            actor=call.context.actor,
            operation_id=f"{call.context.call_id}:wire:{attempt}",
            hook=Hook.BEFORE_LLM_CALL,
            target=target.target_id,
            action="llm.chat.completions",
            payload=payload,
            source_level=snapshot.level,
            source_version=snapshot.version,
        )
        pending = self.guard.prepare(operation)
        if pending is not None:
            with self._lock:
                self._pending.add(pending.approval_id)
        public_target = {
            "provider": target.provider,
            "model": target.model,
            "endpoint": target.endpoint,
            "protocol": target.protocol,
            "attempt": attempt,
        }
        return call, operation, pending, public_target, self.guard.revision

    def _refresh(self, call, operation, revision):
        if self.current_call() is not call or revision != self.guard.revision:
            raise SecurityDenied("模型调用或策略版本已改变")
        state = self.sources.snapshot(operation.actor)
        if state.level != operation.source_level or state.version != operation.source_version:
            raise SecurityDenied("模型请求审批期间来源已改变")
        if self.guard.authorize(operation) is not True:
            raise SecurityDenied("当前主体无权发送模型请求")
        if operation.target not in self.guard.policy.allowed_targets:
            raise SecurityDenied("模型请求目标权限已撤销")

    @staticmethod
    def _public(pending, target):
        public = pending.public()
        public["llm"] = dict(target)
        return public

    def _done(self, pending):
        if pending is not None:
            self.guard.broker.cancel(pending.approval_id)
            with self._lock:
                self._pending.discard(pending.approval_id)

    @contextmanager
    def _audit_sending(self, operation):
        def emit(outcome):
            if self.audit is not None:
                self.audit.emit(
                    actor=operation.actor,
                    hook=operation.hook,
                    operation_id=operation.operation_id,
                    target=operation.target,
                    level=operation.source_level,
                    outcome=outcome,
                )

        emit("started")
        try:
            yield
        except BaseException:
            emit("unknown")
            raise
        else:
            emit("completed")

    @asynccontextmanager
    async def arequest(self, endpoint, body):
        call, pending, sending, token = None, None, False, None
        try:
            call = self.current_call()
            call, operation, pending, target, revision = self._prepare(endpoint, body)
            if pending is None:
                selected = operation.payload
            else:
                presented = self.present(self._public(pending, target))
                if inspect.isawaitable(presented):
                    await presented
                await self.guard.broker.wait(pending.approval_id)
                self._refresh(call, operation, revision)
                selected = self.guard.claim(pending, operation)
            selected = validate_llm_candidate(operation.payload, selected)
            async with self.sources.alease(operation.actor, operation.source_version):
                self._refresh(call, operation, revision)
                token = self._wire.set(operation)
                sending = True
                with self._audit_sending(operation):
                    yield selected
        except BaseException as exc:
            if call is not None:
                if (
                    not sending
                    or isinstance(exc, SecurityDenied)
                    or (not isinstance(exc, Exception))
                ):
                    call.stopped = True
            raise
        finally:
            if token is not None:
                self._wire.reset(token)
            self._done(pending)

    def create_http_client(self, **options):
        from .llm_http import create_http_client

        return create_http_client(self, **options)

    def create_binding(self):
        return self.driver.create_llm_binding(self)

    def register_native_provider(self):
        with self._lock:
            if self._closed:
                raise SecurityDenied("模型适配器已关闭")
            if self._native_provider is None:
                self._native_provider = self.driver.register_llm_provider(self)
            return self._native_provider

    def close(self):
        with self._lock:
            self._closed = True
            for call in self._calls.values():
                call.stopped = True
            for approval_id in self._pending:
                self.guard.broker.cancel(approval_id)
            self._pending.clear()
            self._calls.clear()
            cleanup, self._cleanup = self._cleanup, []
        for release in cleanup:
            release()


def create_llm_adapter(
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
    return LlmAdapter(config.framework, guard, driver=selected, **ports)
