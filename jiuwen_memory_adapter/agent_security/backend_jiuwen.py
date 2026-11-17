"""jiuwen memory 后端：接口名、参数与安全文本视图均由本后端维护。"""

from dataclasses import asdict

from ._validation import has_exact_type
from .backends import BackendOperation, BackendResult, backend_catalog
from .memory import SdkMemoryReader
from .runtime import SecurityDenied, encode_payload


class JiuwenMemoryBackend:
    backend_id = "jiuwen"

    def __init__(self, context, *, api, **native_ports):
        self.reader = SdkMemoryReader(
            api, context.sources, context.inspector, audit=context.audit, **native_ports
        )
        self.operations = tuple(
            BackendOperation(
                op["name"], op["description"], encode_payload(op["schema"]), effect=op["effect"]
            )
            for op in backend_catalog()["backends"][self.backend_id]["operations"]
        )

    def invoke(self, operation, arguments, actor):
        value = self._invoke(operation, arguments, actor)
        return BackendResult(value, self.reader.sources.snapshot(actor))

    def _invoke(self, operation, arguments, actor):
        encode_payload(arguments)
        if not has_exact_type(arguments, dict):
            raise SecurityDenied("jiuwen memory 参数必须是对象")
        if operation == "jiuwen_memory_get" and set(arguments) == {"unit_id"}:
            if (
                not has_exact_type(arguments["unit_id"], str)
                or not 0 < len(arguments["unit_id"]) <= 512
            ):
                raise SecurityDenied("jiuwen memory ID 无效")
            return asdict(self.reader.get(arguments["unit_id"], actor))
        if operation == "jiuwen_memory_list" and not set(arguments) - {"limit"}:
            return [
                asdict(view) for view in self.reader.list(actor, limit=arguments.get("limit", 20))
            ]
        if (
            operation == "jiuwen_memory_search"
            and "query" in arguments
            and not set(arguments) - {"query", "top_k"}
        ):
            views = self.reader.search(arguments["query"], actor, top_k=arguments.get("top_k", 10))
            return [asdict(view) for view in views]
        raise SecurityDenied("jiuwen memory 未暴露此接口或参数")

    def close(self):
        self.reader.close()
