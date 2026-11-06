"""真实 PR406 检测规则与审批状态机的边界验证。"""

import asyncio
import json
from dataclasses import replace

import pytest

from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.runtime import (
    ApprovalChoice,
    ApprovalState,
    SecurityDenied,
)

from .fixtures import ACTOR, ID_CARD, guard, operation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cancel", [False, True])
def test_async_wait_accepts_independent_host_decision(cancel):
    service, op = guard(), operation()
    pending = service.prepare(op)

    async def scenario():
        waiting = asyncio.create_task(service.broker.wait(pending.approval_id))
        await asyncio.sleep(0)
        if cancel:
            service.broker.cancel(pending.approval_id)
            await waiting
            with pytest.raises(SecurityDenied):
                service.claim(pending, op)
        else:
            service.broker.submit(
                pending.approval_id, pending.version, ApprovalChoice.SEND_REDACTED, reviewer=ACTOR
            )
            await waiting
            assert service.claim(pending, op)["body"] == "[PRIVATE]"

    try:
        asyncio.run(scenario())
    finally:
        service.broker.close()


@pytest.mark.parametrize(
    "text,level",
    [
        ("meeting tomorrow", "S1"),
        (ID_CARD, "S2"),
        ("api_key=sample-secret-value", "S3"),
        ("Bearer abcdefghijklmnop123456", "S3"),
    ],
)
def test_pr406_levels_and_redaction(text, level):
    inspection = create_text_inspector().inspect(text)
    assert inspection.level_id == level
    if level != "S1":
        assert inspection.redacted != text
        assert text not in repr(inspection)


def test_inspection_is_independent_of_write_exemption_and_preserves_adjacent_secrets():
    inspector = create_text_inspector()
    assert inspector.inspect(ID_CARD).redacted == "[PRIVATE]"
    assert inspector.inspect("api_key=[PRIVATE]").level_id == "S1"
    assert inspector.inspect("api_key=[PRIVATE]newsecret").level_id == "S3"
    result = inspector.inspect("short-secret", label="api_key")
    assert result.level_id == "S3" and result.redacted == "[PRIVATE]"


def test_three_choices_replace_only_body_and_allow_exactly_one_claim():
    service, op = guard(), operation()
    pending = service.prepare(op)
    assert set(pending.choices) == set(ApprovalChoice)
    assert ID_CARD not in json.dumps(pending.public())
    public = pending.public()
    public["preview"]["body"] = "tampered"
    assert pending.public()["preview"]["body"] == "[PRIVATE]"
    service.broker.submit(
        pending.approval_id, pending.version, ApprovalChoice.SEND_REDACTED, reviewer=ACTOR
    )
    service.broker.submit(
        pending.approval_id, pending.version, ApprovalChoice.SEND_REDACTED, reviewer=ACTOR
    )
    assert service.claim(pending, op) == {"body": "[PRIVATE]", "to": "approved"}
    assert service.broker.state(pending.approval_id) == ApprovalState.CONSUMED
    with pytest.raises(SecurityDenied):
        service.claim(pending, op)
    with pytest.raises(SecurityDenied):
        service.prepare(op)


@pytest.mark.parametrize("choice", [ApprovalChoice.CONTINUE_ORIGINAL, ApprovalChoice.ABORT])
def test_original_and_abort(choice):
    service, op = guard(), operation()
    pending = service.prepare(op)
    service.broker.submit(pending.approval_id, pending.version, choice, reviewer=ACTOR)
    if choice == ApprovalChoice.ABORT:
        with pytest.raises(SecurityDenied):
            service.claim(pending, op)
    else:
        assert service.claim(pending, op) == op.payload


def test_s3_default_disables_original_but_retains_verified_redacted_choice():
    service = guard()
    op = operation({"body": "api_key=secret-value"})
    pending = service.prepare(op)
    assert pending.level == "S3"
    assert set(pending.choices) == {ApprovalChoice.ABORT, ApprovalChoice.SEND_REDACTED}
    with pytest.raises(SecurityDenied):
        service.broker.submit(
            pending.approval_id, pending.version, ApprovalChoice.CONTINUE_ORIGINAL, reviewer=ACTOR
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"actor": replace(ACTOR, session_id="other")},
        {"target": "elsewhere"},
        {"source_version": "changed"},
        {"payload_json": '{"body":"different"}'},
    ],
)
def test_changed_operation_cannot_reuse_approval(changed):
    service, op = guard(), operation()
    pending = service.prepare(op)
    service.broker.submit(
        pending.approval_id, pending.version, ApprovalChoice.CONTINUE_ORIGINAL, reviewer=ACTOR
    )
    with pytest.raises(SecurityDenied):
        service.claim(pending, replace(op, **changed))


def test_wrong_reviewer_version_and_revocation_are_denied():
    service, op = guard(), operation()
    pending = service.prepare(op)
    for reviewer, version in [(replace(ACTOR, user_id="bob"), pending.version), (ACTOR, "old")]:
        with pytest.raises(SecurityDenied):
            service.broker.submit(
                pending.approval_id, version, ApprovalChoice.SEND_REDACTED, reviewer=reviewer
            )
    service.broker.submit(
        pending.approval_id, pending.version, ApprovalChoice.SEND_REDACTED, reviewer=ACTOR
    )

    def deny(_):
        return False

    service.authorize = deny
    with pytest.raises(SecurityDenied):
        service.claim(pending, op)


def test_source_label_is_not_downgraded_and_uneditable_destinations_are_not_changed():
    service = guard()
    pending = service.prepare(operation({"body": "already masked"}, source_level="S3"))
    assert pending.level == "S3"
    pending = service.prepare(operation({"body": "hello", "to": ID_CARD}, operation_id="tool-2"))
    assert ApprovalChoice.SEND_REDACTED not in pending.choices
    assert pending.public()["preview"] is None


def test_expiration_capacity_and_close_fail_closed():
    now = [1.0]
    service, op = guard(clock=lambda: now[0], ttl=5, capacity=1), operation()
    pending = service.prepare(op)
    now[0] = 7
    assert service.broker.state(pending.approval_id) == ApprovalState.EXPIRED
    with pytest.raises(SecurityDenied):
        service.prepare(replace(op, operation_id="tool-2"))
    service.broker.close()
    # 白盒验证关闭时释放所有已领取凭据，避免只检查外部拒绝。
    assert not service.broker._entries  # pylint: disable=protected-access
    with pytest.raises(SecurityDenied):
        service.prepare(op)


def test_async_gate_waits_without_sending_or_calling_model_and_cancellation_closes_request():
    async def scenario():
        service, op = guard(), operation()
        shown, sent = [], []

        async def present(request):
            shown.append(request)

        async def execute():
            selected = await service.gate(op, present=present, refresh=lambda: op)
            sent.append(selected)

        task = asyncio.create_task(execute())
        await asyncio.sleep(0)
        assert len(shown) == 1 and sent == []
        request = shown[0]
        await asyncio.to_thread(
            service.broker.submit,
            request["approval_id"],
            request["version"],
            ApprovalChoice.SEND_REDACTED,
            reviewer=ACTOR,
        )
        await task
        assert sent == [{"body": "[PRIVATE]", "to": "approved"}]

        op = replace(op, operation_id="cancel")
        task = asyncio.create_task(execute())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.broker.state(shown[-1]["approval_id"]) == ApprovalState.CANCELLED

    asyncio.run(scenario())
