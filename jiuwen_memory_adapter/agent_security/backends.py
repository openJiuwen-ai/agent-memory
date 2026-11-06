"""记忆后端注册与原生接口目录；不规定统一 CRUD，也不导入具体记忆 SDK。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Protocol

from ._validation import has_exact_type
from .runtime import Actor, SecurityDenied, encode_payload
from .sources import SourceLedger, SourceSnapshot

if TYPE_CHECKING:
    from .audit import SecurityAuditLog
    from .inspection import TextInspector


@dataclass(frozen=True)
class BackendResult:
    value: object
    source: SourceSnapshot


@dataclass(frozen=True)
class BackendOperation:
    name: str
    description: str
    schema_json: str
    effect: str = "memory-read"

    def __post_init__(self) -> None:
        if self.effect != "memory-read":
            raise SecurityDenied("当前后端目录仅接安全读取；写入或外发操作须另接执行门禁")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", self.name) or not self.description:
            raise SecurityDenied("后端接口名称或描述无效")
        schema = json.loads(self.schema_json)
        if not has_exact_type(schema, dict) or schema.get("type") != "object":
            raise SecurityDenied("后端接口参数必须是 JSON 对象 schema")
        encode_payload(schema)

    @property
    def schema(self) -> dict[str, Any]:
        return json.loads(self.schema_json)


class MemoryBackend(Protocol):
    backend_id: str
    operations: tuple[BackendOperation, ...]

    def invoke(self, operation: str, arguments: dict[str, Any], actor: Actor) -> BackendResult:
        """由显式注册的实现提供此端口。"""

        ...

    def close(self) -> None:
        """由显式注册的实现提供此端口。"""

        ...


@dataclass(frozen=True)
class BackendContext:
    sources: SourceLedger
    inspector: TextInspector
    audit: SecurityAuditLog | None


class BackendFactory(Protocol):
    def __call__(self, context: BackendContext, **native_options: Any) -> MemoryBackend:
        """由显式注册的实现提供此端口。"""
        ...


class MemoryBackendRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, BackendFactory] = {}

    def register(self, backend_id: str, factory: BackendFactory) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend_id) or not callable(factory):
            raise SecurityDenied("记忆后端注册无效")
        if backend_id in self._factories:
            raise SecurityDenied("记忆后端重复注册")
        self._factories[backend_id] = factory

    def create(
        self, backend_id: str, context: BackendContext, **native_options: Any
    ) -> MemoryBackend:
        factory = self._factories.get(backend_id)
        if factory is None:
            raise SecurityDenied("记忆后端未注册")
        backend = factory(context, **native_options)
        try:
            if backend.backend_id != backend_id or not has_exact_type(backend.operations, tuple):
                raise SecurityDenied("记忆后端声明不匹配")
            if any(not isinstance(op, BackendOperation) for op in backend.operations):
                raise SecurityDenied("记忆后端接口必须使用经过验证的 BackendOperation")
            names = [op.name for op in backend.operations]
            if not names or len(set(names)) != len(names) or len(names) > 128:
                raise SecurityDenied("记忆后端接口目录无效")
            return backend
        except BaseException:
            backend.close()
            raise


def backend_catalog():
    return json.loads(
        files(__package__).joinpath("backend_catalog.json").read_text(encoding="utf8")
    )


def verify_backend_result(result: BackendResult, *, actor: Actor, sources: SourceLedger):
    if not isinstance(result, BackendResult) or not isinstance(result.source, SourceSnapshot):
        raise SecurityDenied("后端结果缺少可信来源交付契约")
    value = json.loads(encode_payload(result.value))
    current = sources.verify(actor, result.source.version)
    if current != result.source or (value not in (None, "", [], {}) and not current.references):
        raise SecurityDenied("后端结果没有已登记来源或快照不匹配")
    return value


def create_backend_registry():
    registry = MemoryBackendRegistry()

    def jiuwen(context, **options):
        from .backend_jiuwen import JiuwenMemoryBackend

        return JiuwenMemoryBackend(context, **options)

    registry.register("jiuwen", jiuwen)
    return registry
