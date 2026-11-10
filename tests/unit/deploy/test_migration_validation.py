"""Plaintext migration admits current memory records, never repairs arbitrary bytes."""

import json

import pytest

from deploy.migration.validation import (
    FormatUpgradeRequired,
    validate_plaintext_memory,
)
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.memory_codec import dumps

pytestmark = pytest.mark.unit


def payload():
    return json.loads(
        dumps(
            MemoryUnit(
                id="record-1",
                scope=Scope(org="acme", user="alice"),
                segments=[Segment(content="private memory")],
            )
        )
    )


def test_admits_current_serialized_memory_without_modification():
    raw = json.dumps(payload()).encode()
    unit = validate_plaintext_memory(raw, max_bytes=len(raw))
    assert unit.id == "record-1" and unit.content == "private memory"


@pytest.mark.parametrize(
    "field,value",
    [
        ("_v", 5),
        ("_v", True),
        ("id", ""),
        ("id", 12),
        ("scope", ["acme", "alice"]),
        ("scope", ["", "", "alice", "", ""]),
        ("scope", ["acme", "", 12, "", ""]),
        ("segments", "text"),
        ("segments", [{"content": 12}]),
        ("segments", [{"content": "x", "evil": True}]),
        ("unexpected", "value"),
        ("tier", "unsupported"),
    ],
)
def test_rejects_invalid_schema_and_coercions(field, value):
    value_payload = payload()
    value_payload[field] = value
    with pytest.raises(ValidationError, match="invalid plaintext memory format"):
        validate_plaintext_memory(json.dumps(value_payload).encode(), max_bytes=65536)


@pytest.mark.parametrize("raw", [b"ENC1broken", b"not json", b"[]", b'{"id":1,"id":2}'])
def test_rejects_non_memory_or_duplicate_fields(raw):
    with pytest.raises(ValidationError):
        validate_plaintext_memory(raw, max_bytes=65536)


def test_rejects_oversized_plaintext():
    with pytest.raises(ValidationError):
        validate_plaintext_memory(json.dumps(payload()).encode(), max_bytes=10)


def test_old_format_requires_explicit_upgrade():
    value = payload()
    value["_v"] = 3
    with pytest.raises(FormatUpgradeRequired):
        validate_plaintext_memory(json.dumps(value).encode(), max_bytes=65536)
