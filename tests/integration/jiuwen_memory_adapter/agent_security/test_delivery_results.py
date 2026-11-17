"""SDK 来源、结果脱敏、原会话决定与最终交付的跨边界验证。"""

import asyncio
import json
from dataclasses import replace

import pytest

from jiuwen_memory_adapter.agent_security import ApprovalChoice, Hook, SecurityDenied
from jiuwen_memory_adapter.agent_security.approval import ApprovalInbox
from jiuwen_memory_adapter.agent_security.audit import SecurityAuditLog
from jiuwen_memory_adapter.agent_security.delivery import DeliveryContext, create_delivery_adapter
from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.results import ResultGuard
from jiuwen_memory_adapter.agent_security.runtime import (
    ApprovalBroker,
    DefaultEgressPolicy,
    EgressGuard,
    PayloadInspector,
)

from .fixtures import SAMPLE

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize("hook", [Hook.BEFORE_AGENT_RESPONSE, Hook.BEFORE_AGENT_MESSAGE])
def test_sdk_derived_reply_and_agent_handoff(sdk_source, choice, hook):
    _, sources, _, _, actor, _, _ = sdk_source
    receiver = replace(actor, agent_id="child")
    sources.begin(receiver, fresh=True)
    broker, inspector, audit = ApprovalBroker(), create_text_inspector(), SecurityAuditLog()
    inbox = ApprovalInbox(broker)
    guard = EgressGuard(
        PayloadInspector(inspector, frozenset({"body"})),
        DefaultEgressPolicy(frozenset({"recipient"})),
        broker,
        authorize=lambda _: True,
    )
    shown, sent = [], []

    def present(public, *, actor):
        inbox.present(public, actor=actor)
        shown.extend(inbox.pending(actor))
        inbox.decide(
            actor=actor, approval_id=public["approval_id"], version=public["version"], choice=choice
        )

    recipient = receiver if hook == Hook.BEFORE_AGENT_MESSAGE else None
    adapter = create_delivery_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        context_for=lambda host, event: DeliveryContext(
            actor, "reply-1", "recipient", "reply", recipient
        ),
        present=present,
        authorize_transfer=lambda *args: True,
        audit=audit,
    )

    def deliver(payload, context):
        if recipient:
            assert sources.snapshot(recipient).level == "S2", "先继承来源，再进入接收 agent"
        sent.append(dict(payload))
        return "delivered"

    async def run_async():
        return await adapter.asend(
            host=object(), hook=hook, payload={"body": "ordinary derived summary"}, send=deliver
        )

    try:
        if choice == ApprovalChoice.ABORT:
            with pytest.raises(SecurityDenied):
                asyncio.run(run_async())
            assert not sent
            assert sources.snapshot(receiver).level == "S1"
        else:
            result = asyncio.run(run_async())
            assert result == "delivered"
            assert sent == [{"body": "ordinary derived summary"}]
        assert len(shown) == 1 and shown[0]["level"] == "S2"
        events = audit.drain()
        assert events and "ordinary derived summary" not in json.dumps(events)
        with pytest.raises(SecurityDenied):
            asyncio.run(
                adapter.asend(host=object(), hook=hook, payload={"body": "replay"}, send=deliver)
            )
    finally:
        adapter.close()
        inbox.close()
        broker.close()
        audit.close()


@pytest.mark.parametrize("change", ["revoked", "actor", "new_source", "cross_user", "closed"])
def test_delivery_rechecks_waiting_operation(sdk_source, change):
    runtime, sources, reader, allowed, actor, scope, _ = sdk_source
    current, sent = {"actor": actor}, []
    broker, inspector = ApprovalBroker(), create_text_inspector()
    inbox = ApprovalInbox(broker)
    guard = EgressGuard(
        PayloadInspector(inspector, frozenset({"body"})),
        DefaultEgressPolicy(frozenset({"user"})),
        broker,
        authorize=lambda _: True,
    )

    async def present(public, *, actor):
        inbox.present(public, actor=actor)
        if change == "revoked":
            allowed["value"] = False
        elif change == "actor":
            current["actor"] = replace(actor, user_id="mallory")
        elif change == "new_source":
            from .fixtures import sdk_security

            unit = await asyncio.to_thread(
                runtime.api.add, "api_key=new-secret", scope, security=sdk_security(scope)
            )
            await reader.aget(unit[0].id, actor)
        elif change == "closed":
            adapter.close()
        inbox.decide(
            actor=replace(actor, user_id="mallory") if change == "cross_user" else actor,
            approval_id=public["approval_id"],
            version=public["version"],
            choice=ApprovalChoice.SEND_REDACTED,
        )

    adapter = create_delivery_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        context_for=lambda *args: DeliveryContext(current["actor"], "final", "user", "response"),
        present=present,
    )
    try:
        with pytest.raises(SecurityDenied):
            asyncio.run(
                adapter.asend(
                    host=None,
                    hook=Hook.BEFORE_AGENT_RESPONSE,
                    payload={"body": SAMPLE},
                    send=lambda p, c: sent.append(p),
                )
            )
        assert not sent
    finally:
        adapter.close()
        inbox.close()
        broker.close()


def test_result_is_redacted_before_release_and_origin_remains_revocable(sdk_source):
    _, sources, _, _, actor, _, _ = sdk_source
    allowed = {"value": True}
    guard = ResultGuard(
        sources, create_text_inspector(), authorize_input=lambda *args: allowed["value"]
    )
    try:
        view = guard.observe(
            actor,
            hook=Hook.AFTER_TOOL_CALL,
            producer="tool",
            operation_id="result-1",
            value={"body": SAMPLE},
        )
        assert view == {"body": "[PRIVATE]"}
        snapshot = sources.snapshot(actor)
        assert snapshot.level == "S2"
        assert SAMPLE not in repr(snapshot)
        allowed["value"] = False
        with pytest.raises(SecurityDenied):
            with sources.lease(actor, snapshot.version):
                pytest.fail("失效来源不得继续外发")
    finally:
        guard.close()
