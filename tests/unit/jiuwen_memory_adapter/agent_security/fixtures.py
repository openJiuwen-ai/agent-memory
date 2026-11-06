"""确定性的宿主身份与工具操作辅助，不代替真实认证。"""

from dataclasses import replace

from jiuwen_memory_adapter.agent_security.hooks import Hook
from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.runtime import (
    Actor,
    ApprovalBroker,
    DefaultEgressPolicy,
    EgressGuard,
    Operation,
    PayloadInspector,
)

ACTOR = Actor("tenant", "alice", "conversation", "assistant", "run-1")
ID_CARD = "11010519491231002X"


def operation(payload=None, **changes):
    value = Operation.create(
        operation_id="tool-1",
        actor=ACTOR,
        hook=Hook.BEFORE_TOOL_CALL,
        target="mail-approved",
        action="send_mail",
        source_level="S1",
        source_version="0",
        payload={"body": ID_CARD, "to": "approved"} if payload is None else payload,
    )
    return replace(value, **changes)


def guard(**broker_options):
    return EgressGuard(
        PayloadInspector(create_text_inspector(), frozenset({"body"})),
        DefaultEgressPolicy(frozenset({"mail-approved"})),
        ApprovalBroker(**broker_options),
        authorize=lambda _: True,
    )
