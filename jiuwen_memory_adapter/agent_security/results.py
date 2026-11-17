"""结果进入框架前的不可逆文本视图与来源记录，不保留原始执行结果。"""

import asyncio
import hashlib
import hmac
import json
import secrets
import threading

from ._validation import has_exact_type
from .hooks import Hook
from .runtime import PayloadInspector, SecurityDenied, encode_payload
from .sources import SourceReference, strongest

_RESULT_HOOKS = {Hook.AFTER_LLM_CALL, Hook.AFTER_TOOL_CALL, Hook.BEFORE_AGENT_MESSAGE}
_MEDIA = {
    "image",
    "image_url",
    "input_image",
    "audio",
    "input_audio",
    "video",
    "file",
    "document",
    "media",
    "attachment",
}


def text_result(value):
    """只接受可完整检查的 JSON；附件/二进制块不能伪装为普通文本结果。"""
    value = json.loads(encode_payload(value))

    def visit(item):
        if has_exact_type(item, dict):
            if item.get("type") in _MEDIA or any(
                k in item for k in ("attachments", "mediaUrl", "mediaUrls", "image_url", "file_id")
            ):
                raise SecurityDenied("附件与多模态结果未覆盖")
            for child in item.values():
                visit(child)
        elif has_exact_type(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


class ResultGuard:
    """只保存结果的引用、等级和带密钥摘要；权限由当前宿主输入授权端口复核。"""

    def __init__(self, sources, inspector, *, authorize_input, audit=None, capacity=4096):
        if not has_exact_type(capacity, int) or capacity < 1:
            raise ValueError("结果来源容量必须为正整数")
        self.sources, self.inspector = sources, inspector
        self.authorize_input, self.audit, self.capacity = authorize_input, audit, capacity
        self._scanner = PayloadInspector(inspector, frozenset({"value"}))
        self._secret, self._records = secrets.token_bytes(32), {}
        self._provider = "agent-results-" + secrets.token_hex(12)
        self._lock, self._closed = threading.RLock(), False
        self.sources.register_provider(self._provider, self._validate)

    def _validate(self, actor, reference):
        with self._lock:
            item = self._records.get(reference)
            if (
                self._closed
                or item is None
                or reference.presentation_version != self.inspector.version
            ):
                return False
        hook, producer = item
        return self.authorize_input(actor, hook, producer) is True

    def observe(self, actor, *, hook, producer, operation_id, value):
        hook = Hook(hook)
        if hook not in _RESULT_HOOKS or self.authorize_input(actor, hook, producer) is not True:
            raise SecurityDenied("结果使用未获授权")
        original = text_result(value)
        before = self.sources.snapshot(actor)
        self.sources.verify(actor, before.version)
        version = self.inspector.version
        inspection = self._scanner.inspect({"value": original})
        if inspection.redacted_json is None:
            raise SecurityDenied("结果无法构造安全文本视图")
        candidate = json.loads(inspection.redacted_json)["value"]
        checked = self._scanner.inspect({"value": candidate})
        if (
            self.inspector.version != version
            or checked.level != "S1"
            or checked.redacted_json != inspection.redacted_json
        ):
            raise SecurityDenied("结果安全视图复检失败")
        digest = hmac.new(
            self._secret,
            encode_payload(
                [vars(actor), str(hook), producer, operation_id, original, version]
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        reference = SourceReference(
            self._provider,
            (actor.tenant_id, "", actor.user_id, actor.agent_id, actor.session_id),
            digest,
            digest,
            strongest(before.level, inspection.level),
            str(hook),
            version,
            digest,
        )
        with self._lock:
            if self._closed or (
                reference not in self._records and len(self._records) >= self.capacity
            ):
                raise SecurityDenied("结果来源提供方不可用或容量已满")
            self._records[reference] = (hook, producer)
            snapshot = self.sources.record(actor, [reference])
            self.sources.verify(actor, snapshot.version)
            if self.audit is not None:
                self.audit.emit(
                    actor=actor,
                    hook=hook,
                    operation_id=operation_id,
                    target=producer,
                    level=reference.level,
                    outcome="redacted",
                )
        return candidate

    async def aobserve(self, actor, **values):
        return await asyncio.to_thread(self.observe, actor, **values)

    def ensure(self, actor, *, hook, producer, operation_id, value):
        """框架消费边界的复核；已安全的 transport 结果不重复登记/派发 AFTER。"""
        original = text_result(value)
        checked = self._scanner.inspect({"value": original})
        current = self.sources.snapshot(actor)
        self.sources.verify(actor, current.version)
        if checked.level == "S1" and checked.redacted_json == encode_payload({"value": original}):
            if self.authorize_input(actor, Hook(hook), producer) is not True:
                raise SecurityDenied("结果使用权限已撤销")
            return original
        return self.observe(
            actor, hook=hook, producer=producer, operation_id=operation_id, value=original
        )

    def release_stream(self, actor, *, producer, operation_id, value):
        if self.authorize_input(actor, Hook.ON_LLM_STREAM_CHUNK, producer) is not True:
            raise SecurityDenied("模型流释放未获授权")
        candidate = self._scanner.inspect({"value": text_result(value)})
        if candidate.level != "S1" or candidate.redacted_json != encode_payload({"value": value}):
            raise SecurityDenied("待释放模型流含未处理内容")
        current = self.sources.snapshot(actor)
        self.sources.verify(actor, current.version)
        if self.audit is not None:
            self.audit.emit(
                actor=actor,
                hook=Hook.ON_LLM_STREAM_CHUNK,
                operation_id=operation_id,
                target=producer,
                level=current.level,
                outcome="released",
            )
        return value

    def close(self):
        with self._lock:
            self._closed = True
            self._records.clear()
        self.sources.unregister_provider(self._provider, self._validate)
