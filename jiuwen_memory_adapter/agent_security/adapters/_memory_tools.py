"""把后端自带的接口目录装配为框架工具；不解释 get/search 等后端语义。"""

import asyncio

from ..backends import verify_backend_result
from ..hooks import Hook
from ..runtime import SecurityDenied, encode_payload


def create_memory_tools(plugin, backend):
    def create(operation):
        def invoke(**arguments):
            try:
                run = plugin.current_run()
                value = verify_backend_result(
                    backend.invoke(operation.name, arguments, run.actor),
                    actor=run.actor,
                    sources=plugin.sources,
                )
                safe = plugin.results.ensure(
                    run.actor,
                    hook=Hook.AFTER_TOOL_CALL,
                    producer=f"memory-backend:{backend.backend_id}",
                    operation_id=f"{run.turn}:{operation.name}",
                    value=value,
                )
                return encode_payload(safe)
            except Exception:
                raise SecurityDenied("记忆后端调用未完成或安全视图不可用") from None

        async def ainvoke(**arguments):
            return await asyncio.to_thread(invoke, **arguments)

        return plugin.driver.create_tool(
            name=operation.name,
            description=operation.description,
            schema=operation.schema,
            ainvoke=ainvoke,
        )

    return tuple(create(operation) for operation in backend.operations)
