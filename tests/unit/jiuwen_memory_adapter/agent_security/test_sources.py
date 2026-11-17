"""来源状态不可降级、不可跨上下文伪造，交付与外发具有明确互斥点。"""

import asyncio
import threading
from dataclasses import replace

import pytest

from jiuwen_memory_adapter.agent_security import SecurityDenied
from jiuwen_memory_adapter.agent_security.sources import SourceLedger, SourceReference

from .fixtures import ACTOR

pytestmark = pytest.mark.unit


def reference(level="S2", unit="private-unit", org="tenant"):
    return SourceReference("sdk", (org, "", "alice", "", ""), unit, "a" * 32, level, "use")


def ledger(**kwargs):
    sources = SourceLedger(**kwargs)
    sources.register_provider("sdk", lambda actor, ref: True)
    sources.begin(ACTOR, fresh=True)
    return sources


def test_accumulates_original_sources_without_downgrading_or_reset():
    sources = ledger()
    first = sources.record(ACTOR, [reference("S3")])
    assert sources.record(ACTOR, [reference("S3")]) == first
    later = sources.record(ACTOR, [reference("S1", "public")])
    assert later.level == "S3" and later.version != first.version
    assert "private-unit" not in repr(later)
    with pytest.raises(SecurityDenied):
        sources.begin(ACTOR, fresh=True)
    sources.retire(ACTOR)
    with pytest.raises(SecurityDenied):
        sources.begin(ACTOR, fresh=True)
    with pytest.raises(SecurityDenied):
        sources.snapshot(ACTOR)


def test_unknown_history_provider_or_tenant_is_never_public():
    sources = SourceLedger()
    with pytest.raises(SecurityDenied):
        sources.snapshot(ACTOR)
    with pytest.raises(SecurityDenied):
        sources.begin(ACTOR, fresh=False)
    sources.begin(ACTOR, fresh=True)
    for ref in [reference(), reference(org="other")]:
        with pytest.raises(SecurityDenied):
            sources.record(ACTOR, [ref])
    assert sources.snapshot(ACTOR).level == "S1"


def test_state_transfer_requires_recipient_permission_and_preserves_level():
    sources = ledger()
    sources.record(ACTOR, [reference("S3")])
    child = replace(ACTOR, agent_id="child", incarnation="child-run")
    sources.begin(child, fresh=True)
    with pytest.raises(SecurityDenied):
        sources.inherit(ACTOR, child, authorize_transfer=lambda *args: False)
    assert sources.snapshot(child).level == "S1"
    received = sources.inherit(ACTOR, child, authorize_transfer=lambda *args: True)
    assert received.level == "S3" and received.references == sources.snapshot(ACTOR).references
    with pytest.raises(SecurityDenied):
        sources.inherit(
            ACTOR, replace(child, tenant_id="other"), authorize_transfer=lambda *a: True
        )


def test_capacity_failure_does_not_partially_record_and_retirement_has_tombstone():
    sources = ledger(capacity=1, reference_capacity=1)
    original = sources.snapshot(ACTOR)
    with pytest.raises(SecurityDenied):
        sources.record(ACTOR, [reference(), reference(unit="other")])
    assert sources.snapshot(ACTOR) == original
    sources.retire(ACTOR)
    with pytest.raises(SecurityDenied):
        sources.begin(replace(ACTOR, incarnation="new"), fresh=True)


def test_egress_lease_blocks_new_delivery_and_rechecks_revocation():
    sources = SourceLedger()
    permission = {"allow": True}
    sources.register_provider("sdk", lambda *args: permission["allow"])
    initial = sources.begin(ACTOR, fresh=True)
    current = sources.record(ACTOR, [reference()])
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, initial.version):
            pytest.fail("旧版本不能开始发送")
    with sources.lease(ACTOR, current.version):
        with pytest.raises(SecurityDenied):
            sources.record(ACTOR, [reference("S3", "new")])
        with pytest.raises(SecurityDenied):
            sources.retire(ACTOR)
    permission["allow"] = False
    with pytest.raises(SecurityDenied):
        with sources.lease(ACTOR, current.version):
            pytest.fail("已撤销来源不能继续使用")


def test_cancelled_async_validation_cannot_leak_an_execution_lease():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        sources = SourceLedger()
        blocking = {"enabled": False}

        def validate(*args):
            if blocking["enabled"]:
                entered.set()
                assert release.wait(5), "测试校验器未获释放"
            return True

        sources.register_provider("sdk", validate)
        sources.begin(ACTOR, fresh=True)
        current = sources.record(ACTOR, [reference()])
        blocking["enabled"] = True

        async def send():
            async with sources.alease(ACTOR, current.version):
                pytest.fail("取消后不能进入发送区间")

        task = asyncio.create_task(send())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            blocking["enabled"] = False
            release.set()
        assert sources.record(ACTOR, [reference("S3", "later")]).level == "S3"

    asyncio.run(scenario())


def test_provider_unload_during_validation_prevents_first_delivery_commit():
    sources = SourceLedger()
    sources.begin(ACTOR, fresh=True)

    def validate(*args):
        sources.unregister_provider("sdk", validate)
        return True

    sources.register_provider("sdk", validate)
    with pytest.raises(SecurityDenied):
        sources.record(ACTOR, [reference()])
    assert sources.snapshot(ACTOR).references == ()
