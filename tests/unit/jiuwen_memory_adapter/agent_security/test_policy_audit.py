"""目标策略与审计故障不能变成发送许可。"""

from dataclasses import replace

import pytest

from jiuwen_memory_adapter.agent_security import (
    Actor,
    EgressPolicy,
    Hook,
    SecurityDenied,
    TargetRule,
)
from jiuwen_memory_adapter.agent_security.audit import SecurityAuditLog
from jiuwen_memory_adapter.agent_security.runtime import (
    ApprovalBroker,
    EgressGuard,
    Operation,
    PayloadInspector,
)

pytestmark = pytest.mark.unit


def test_audit_is_actor_scoped_bounded_and_contains_no_business_identifiers():
    log = SecurityAuditLog(capacity=2)
    actor = Actor("tenant", "alice", "session", "main", "1")
    other = replace(actor, user_id="bob")
    values = dict(
        hook=Hook.BEFORE_TOOL_CALL,
        operation_id="sensitive-operation",
        target="sensitive-destination",
        level="S2",
        outcome="prepared",
    )
    log.emit(actor=actor, **values)
    log.emit(actor=other, **values)
    with pytest.raises(SecurityDenied):
        log.emit(actor=actor, **values)
    own = log.drain(actor=actor)
    assert len(own) == 1
    assert all("sensitive" not in str(value) for value in own[0].values())
    assert len(log.drain(actor=other)) == 1
    assert not log.drain()


def test_sink_failure_stops_gate_before_approval():
    from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector

    def unavailable(event):
        raise RuntimeError("backend may contain a secret")

    log = SecurityAuditLog(sink=unavailable)
    broker = ApprovalBroker()
    guard = EgressGuard(
        PayloadInspector(create_text_inspector(), frozenset({"body"})),
        EgressPolicy([TargetRule("collector")]),
        broker,
        authorize=lambda operation: True,
        audit=log,
    )
    operation = Operation.create(
        actor=Actor("tenant", "alice", "session", "main", "1"),
        operation_id="call",
        hook=Hook.BEFORE_TOOL_CALL,
        target="collector",
        action="send",
        payload={"body": "ordinary summary"},
        source_level="S2",
        source_version="v1",
    )
    with pytest.raises(SecurityDenied, match="审计写入失败"):
        guard.prepare(operation)
    assert not log.drain()


def test_target_rule_restricts_hook_action_and_source_level():
    rule = TargetRule(
        "collector",
        hooks=frozenset({Hook.BEFORE_TOOL_CALL}),
        actions=frozenset({"send"}),
        max_level="S2",
    )
    policy = EgressPolicy([rule])
    operation = Operation.create(
        actor=Actor("tenant", "alice", "session", "main", "1"),
        operation_id="call",
        hook=Hook.BEFORE_TOOL_CALL,
        target="collector",
        action="send",
        payload={"body": "summary"},
        source_level="S2",
        source_version="v1",
    )
    assert policy.permits(operation)
    assert not policy.permits(replace(operation, hook=Hook.BEFORE_AGENT_MESSAGE))
    assert not policy.permits(replace(operation, action="unknown"))
    assert not policy.permits(replace(operation, source_level="S3"))
