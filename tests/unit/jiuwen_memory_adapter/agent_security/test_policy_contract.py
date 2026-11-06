"""相同三级决策与拒绝用例，以共享默认值代替跨语言 JSON 重复展开。"""

from dataclasses import replace

import pytest

from jiuwen_memory_adapter.agent_security import EgressPolicy, SecurityDenied
from jiuwen_memory_adapter.agent_security.runtime import PayloadInspection

from .fixtures import operation

pytestmark = pytest.mark.unit
POLICIES = {
    "default": {"targets": [{"target": "out"}]},
    "redact_only": {
        "schema_version": 1,
        "version": "strict",
        "targets": [{"target": "out", "allow_original": False}],
    },
    "s2_cap": {"targets": [{"target": "out", "max_level": "S2"}]},
    "s1_cap": {"targets": [{"target": "out", "max_level": "S1"}]},
    "one_action": {
        "targets": [{"target": "out", "actions": ["send"], "hooks": ["BEFORE_TOOL_CALL"]}]
    },
    "unicode": {
        "version": "规则😀",
        "targets": [
            {"target": "😀"},
            {"target": "�"},
            {"target": "out", "actions": ["😀", "�", "send"]},
        ],
    },
}
CASES = [
    ("default", "S1", "S2", False, {}, ["CONTINUE_ORIGINAL", "ABORT"]),
    ("default", "S1", "S3", False, {}, None),
    ("redact_only", "S2", "S2", True, {}, ["ABORT", "SEND_REDACTED"]),
    ("redact_only", "S2", "S1", False, {}, None),
    ("s2_cap", "S3", "S1", True, {}, None),
    ("s2_cap", "S1", "S3", True, {}, None),
    ("s1_cap", "S1", "S1", True, {}, ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"]),
    ("s1_cap", "S2", "S1", True, {}, None),
    ("s1_cap", "S1", "S2", True, {}, None),
    ("one_action", "S1", "S2", True, {}, ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"]),
    ("one_action", "S1", "S2", True, {"action": "delete"}, None),
    ("one_action", "S1", "S2", True, {"hook": "BEFORE_LLM_CALL"}, None),
    ("default", "S1", "S1", True, {"target": "unknown"}, None),
    (
        "default",
        "S2",
        "S1",
        True,
        {"hook": "BEFORE_LLM_CALL"},
        ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"],
    ),
    (
        "default",
        "S2",
        "S1",
        True,
        {"hook": "BEFORE_AGENT_RESPONSE"},
        ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"],
    ),
    (
        "default",
        "S2",
        "S1",
        True,
        {"hook": "BEFORE_AGENT_MESSAGE"},
        ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"],
    ),
    ("unicode", "S1", "S2", True, {}, ["CONTINUE_ORIGINAL", "ABORT", "SEND_REDACTED"]),
]
INVALID = [
    {},
    {"schema_version": 2, "targets": [{"target": "out"}]},
    {"schema_version": True, "targets": [{"target": "out"}]},
    {"schema_version": None, "targets": [{"target": "out"}]},
    {"version": "", "targets": [{"target": "out"}]},
    {"version": None, "targets": [{"target": "out"}]},
    {"targets": []},
    {"targets": [{"target": "out"}, {"target": "out"}]},
    {"targets": [{"target": "out", "allow_original": "false"}]},
    {"targets": [{"target": "out", "allow_original": None}]},
    {"targets": [{"target": "out", "max_level": "S4"}]},
    {"targets": [{"target": "out", "max_level": None}]},
    {"targets": [{"target": "out", "actions": "send"}]},
    {"targets": [{"target": "out", "hooks": "BEFORE_TOOL_CALL"}]},
    {"targets": [{"target": "out", "actions": None}]},
    {"targets": [{"target": "out", "hooks": []}]},
    {"targets": [{"target": "out", "actions": ["send", "send"]}]},
    {"targets": [{"target": "out", "hooks": ["AFTER_TOOL_CALL"]}]},
    {"targets": [{"target": "out", "unknown": True}]},
    {"unknown": True, "targets": [{"target": "out"}]},
    {"targets": [None]},
    {"targets": [{"target": 3}]},
    {"targets": [{"target": "out", "actions": [3]}]},
]


@pytest.mark.parametrize("source", ["S1", "S2", "S3"])
@pytest.mark.parametrize("level", ["S1", "S2", "S3"])
def test_default_policy_matrix(source, level):
    op = replace(operation(), target="out", source_level=source)
    choices = EgressPolicy.from_mapping(POLICIES["default"]).choices(
        op, PayloadInspection(level, "{}", (), ())
    )
    expected = ["ABORT", "SEND_REDACTED"]
    if level != "S3":
        expected.insert(0, "CONTINUE_ORIGINAL")
    assert list(choices) == expected


@pytest.mark.parametrize("policy,source,level,redacted,changes,expected", CASES)
def test_policy_boundaries(policy, source, level, redacted, changes, expected):
    op = replace(
        operation(), **{"target": "out", "action": "send", "source_level": source, **changes}
    )
    inspected = PayloadInspection(level, "{}" if redacted else None, (), ())
    configured = EgressPolicy.from_mapping(POLICIES[policy])
    if expected is None:
        with pytest.raises(SecurityDenied):
            configured.choices(op, inspected)
    else:
        assert list(configured.choices(op, inspected)) == expected


@pytest.mark.parametrize("value", INVALID)
def test_invalid_policy(value):
    with pytest.raises(SecurityDenied):
        EgressPolicy.from_mapping(value)
