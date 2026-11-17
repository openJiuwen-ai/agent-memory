"""工具执行体先阻断发送，原生 after Rail 再交付中断，避开工具异常吞并路径。"""

import copy
import inspect
import json

from ..execution import ExecutionAdapter, ExecutionContext, _Frame, _object
from ..runtime import SecurityDenied, encode_payload


class OpenJiuwenExecutionAdapter(ExecutionAdapter):
    framework = "openjiuwen"

    def create_binding(self):
        return self.create_rail()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resumes = {}

    def create_rail(self):
        from openjiuwen.core.single_agent.interrupt import InterruptRequest
        from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail

        owner = self
        names, resumes, frames = self._names, self._resumes, self._frames

        class ExecutionRail(BaseInterruptRail):
            # 优先恢复私有引用供后续参数变换使用；安全性仍由最终执行体保证。
            priority = 1_000_000

            async def before_tool_call(self, ctx):
                name = ctx.inputs.tool_name
                if name not in names:
                    return
                call_id = ctx.inputs.tool_call.id
                frame = _Frame(ExecutionContext(owner.framework, name, call_id, ctx))
                try:
                    incoming = ctx.inputs.tool_args
                    incoming = _object(
                        json.loads(incoming) if isinstance(incoming, str) else incoming
                    )
                    native_reply = self._get_user_input(ctx, call_id)
                    if native_reply is not None:
                        native_reply = _object(native_reply)
                        # 发布版恢复原始工具参数；原生路由后的答复才是恢复引用的真源。
                        # 原始载荷仍从有界存储恢复，后续改写/准备及最终批准绑定照常复核。
                        if "approval_id" not in incoming:
                            incoming = {"approval_id": native_reply.get("approval_id")}
                    if "approval_id" in incoming:
                        if set(incoming) != {"approval_id"}:
                            raise SecurityDenied("中断恢复参数不匹配")
                        saved = resumes.get(incoming["approval_id"])
                        if saved is None:
                            raise SecurityDenied("审批恢复状态不存在")
                        initial_json, original = saved
                        if (name, call_id) != (
                            original.operation.action,
                            original.operation.operation_id,
                        ):
                            raise SecurityDenied("恢复调用不匹配")
                        frame.initial_json = initial_json
                        ctx.inputs.tool_args = initial_json
                        frame.reply = native_reply
                    else:
                        frame.initial_json = encode_payload(incoming)
                    frame.token = frames.set(frame)
                except Exception:
                    self._skip_tool(ctx, ctx.inputs.tool_call, {"status": "denied"})

            async def after_tool_call(self, ctx):
                frame = frames.get()
                if frame is None or frame.context.host is not ctx:
                    return
                frames.reset(frame.token)
                if frame.pending is None:
                    return
                entry = frame.pending
                public = entry.request.public()
                resumes[entry.request.approval_id] = (frame.initial_json, entry)
                safe_call = copy.deepcopy(ctx.inputs.tool_call)
                safe_call.arguments = json.dumps({"approval_id": entry.request.approval_id})
                self._raise_interrupt(
                    frame.context.name,
                    safe_call,
                    InterruptRequest(
                        message=public["message"],
                        payload_schema={
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["approval_id", "version", "choice"],
                            "properties": {
                                "approval_id": {"const": entry.request.approval_id},
                                "version": {"const": entry.request.version},
                                "choice": {"enum": [str(c) for c in entry.request.choices]},
                            },
                        },
                        ui_options=public["options"],
                        metadata={"jiuwen_security": public},
                    ),
                )

        return ExecutionRail()

    def create_tool(self, *, name, description, input_schema, send, prepare_request=None):
        from openjiuwen.core.foundation.tool import Tool, ToolCard

        self._register(name)
        owner = self
        frame_for = self._frame
        prepare = self._aprepare
        select = self._select
        cancel = self._cancel
        sending = self._asending
        check_result = self._aresult
        frames = self._frames
        resumes = self._resumes

        class ProtectedTool(Tool):
            def __init__(self):
                super().__init__(
                    ToolCard(name=name, description=description, input_params=input_schema)
                )

            async def invoke(self, inputs, session=None, **kwargs):
                entry = None
                try:
                    frame = frame_for(name)
                    if session is not frame.context.host.session:
                        raise SecurityDenied("工具会话关联不匹配")
                    entry = await prepare(frame, inputs, prepare_request)
                    if entry.request is not None and frame.reply is None:
                        frame.pending = entry
                        return {"status": "security_approval_required"}
                    selected = select(frame, entry, frame.reply)
                    if entry.request is not None:
                        resumes.pop(entry.request.approval_id, None)
                except SecurityDenied:
                    if entry is not None:
                        cancel(entry)
                    return {"status": "denied", "reason": "安全确认未批准"}
                try:
                    async with sending(frame, entry):
                        result = send(selected, frame.context)
                        result = await result if inspect.isawaitable(result) else result
                    return await check_result(entry, result)
                except SecurityDenied:
                    cancel(entry)
                    return {"status": "withheld", "reason": "执行或结果检查未完成，请勿自动重试"}
                except Exception:
                    if owner.results is None:
                        raise
                    return {"status": "error", "reason": "工具执行失败；结果未释放"}

            async def stream(self, inputs, **kwargs):
                # 当前原生 ReAct 使用 invoke；不把未交付的审批占位符释放为工具结果。
                result = await self.invoke(inputs, **kwargs)
                frame = frames.get()
                if frame is None or frame.pending is None:
                    yield result

        return ProtectedTool()

    def close(self):
        super().close()
        self._resumes.clear()
