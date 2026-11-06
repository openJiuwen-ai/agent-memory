"""完整链路的协议模型端；框架、SDK 和审批仍使用实际实现。"""

import json
from typing import NamedTuple
from urllib.parse import urljoin

from jiuwen_memory_adapter.agent_security import Actor, SecurityPlugin

from .fixtures import SAMPLE


def responder(body, path):
    messages = body["messages"]
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    tool_messages = [m for m in messages if m["role"] == "tool"]
    if "child-job" in first_user:
        message, reason = {"role": "assistant", "content": "child result " + SAMPLE}, "stop"
    elif len(tool_messages) < 3:
        name, args = [
            ("jiuwen_memory_list", {}),
            ("send_report", {"body": "ordinary derived summary", "to": "approved"}),
            ("delegate_worker", {"body": "child-job"}),
        ][len(tool_messages)]
        message, reason = (
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "pipeline-call-" + str(len(tool_messages)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            "tool_calls",
        )
    else:
        message, reason = {"role": "assistant", "content": "final " + SAMPLE}, "stop"
    if body.get("stream"):
        delta = dict(message)
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, **call} for i, call in enumerate(delta["tool_calls"])
            ]
        data = {
            "id": "chatcmpl-pipeline",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        terminal = {**data, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
        return {
            "type": "text/event-stream",
            "chunks": [
                "data: " + json.dumps(data) + "\n\n",
                "data: " + json.dumps(terminal) + "\n\n",
                "data: [DONE]\n\n",
            ],
        }
    return {
        "type": "application/json",
        "chunks": [
            json.dumps(
                {
                    "id": "chatcmpl-pipeline",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                }
            )
        ],
    }


class PipelineBundle(NamedTuple):
    plugin: SecurityPlugin
    actor: Actor
    model: object
    memory_tool: object
    tool: object
    shown: list
    sent: list


def bundle(
    sdk_source,
    root,
    framework,
    choice,
    *,
    interactive=False,
    runtime_registry=None,
    redact_only=False,
):
    from jiuwen_memory_adapter.agent_security import (
        ApprovalChoice,
        EgressPolicy,
        LlmTarget,
        TargetRule,
    )
    from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector

    from .fixtures import sdk_security

    runtime, _, _, _, actor, scope, _ = sdk_source
    shown, sent = [], []
    plugin = None

    def notify(owner, event):
        for request in plugin.pending(owner):
            if any(r["approval_id"] == request["approval_id"] for r in shown):
                continue
            shown.append(request)
            if interactive:
                continue
            selected = (
                choice
                if request["action"] in {"send_report", "agent.response"}
                else ApprovalChoice.SEND_REDACTED
            )
            plugin.decide(
                actor=owner,
                approval_id=request["approval_id"],
                version=request["version"],
                choice=selected,
            )

    inspector = create_text_inspector()
    target_ids = ("model", "current-user", "local-collector", "agent:main", "agent:worker")
    policy = EgressPolicy(
        [TargetRule(target, allow_original=not redact_only) for target in target_ids]
    )
    plugin = SecurityPlugin(
        {"framework": framework, "memory_backend": "jiuwen"},
        inspector=inspector,
        policy=policy,
        targets=[LlmTarget("model", "fixture", "model-a", urljoin(root, "/v1/chat/completions"))],
        authorize=lambda operation: True,
        authorize_input=lambda *args: True,
        authorize_transfer=lambda *args: True,
        notify=notify,
        runtime_registry=runtime_registry,
    )
    reader = plugin.connect_memory_backend(
        api=runtime.api,
        provider="sdk",
        security_for=lambda _: sdk_security(scope),
        scope_for=lambda _: scope,
        authorize_use=lambda *args: True,
        detector_version_for=lambda _: inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    memory_tool = plugin.memory_tools(reader)[1]
    model = plugin.model("model", api_key="fixture-auth", max_retries=0)

    def send(payload, context):
        sent.append(dict(payload))
        return {"body": "api_key=result-secret-value"}

    tool = plugin.tool(
        target="local-collector",
        name="send_report",
        description="本地收集报告",
        input_schema={
            "type": "object",
            "properties": {"body": {"type": "string"}, "to": {"type": "string"}},
            "required": ["body", "to"],
            "additionalProperties": False,
        },
        send=send,
    )
    return PipelineBundle(plugin, actor, model, memory_tool, tool, shown, sent)
