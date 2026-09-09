# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP/CLI 只放行写入接口已有的批级 coords 对象契约。"""

from copy import deepcopy

import pytest

from jiuwen_memory.api import ValidationError
from jiuwen_memory_entry.core.api_contract import parse_request

pytestmark = pytest.mark.unit
_WRITE_METHODS = ("add", "add_async", "batch_add", "batch_add_async")


def _write_payload(method, metadata):
    payload = {"scope": {"org": "local"}, "system_metadata": metadata}
    if method.startswith("batch_"):
        payload["items"] = [{"content": "remember"}]
    else:
        payload["content"] = "remember"
    return payload


@pytest.mark.parametrize("method", _WRITE_METHODS)
@pytest.mark.parametrize("coords", [{}, {"team": "t"}])
def test_write_coords_are_preserved_without_mutating_the_request(method, coords) -> None:
    payload = _write_payload(method, {"coords": coords, "infer": "true", "labels": ["x"]})
    original = deepcopy(payload)

    arguments = parse_request(method, payload)

    assert arguments["system_metadata"] == original["system_metadata"]
    arguments["system_metadata"]["coords"]["team"] = "changed"
    assert payload == original


@pytest.mark.parametrize("method", _WRITE_METHODS)
@pytest.mark.parametrize("coords", [None, [], "{}", {"team": 1}, {"team": {}}, {1: "t"}])
def test_write_coords_reject_non_string_object_entries(method, coords) -> None:
    with pytest.raises(ValidationError, match="coords"):
        parse_request(method, _write_payload(method, {"coords": coords}))


@pytest.mark.parametrize("method", _WRITE_METHODS)
@pytest.mark.parametrize("metadata", [None, {}, {"infer": "true"}])
def test_write_without_coords_retains_existing_metadata_contract(method, metadata) -> None:
    arguments = parse_request(method, _write_payload(method, metadata))

    assert arguments["system_metadata"] == metadata


@pytest.mark.parametrize("field", ["user_metadata", "system_metadata"])
def test_coords_exception_does_not_allow_arbitrary_nested_metadata(field) -> None:
    payload = _write_payload("add", {"coords": {"team": "t"}})
    payload[field] = {"nested": {"team": "t"}}

    with pytest.raises(ValidationError, match=field):
        parse_request("add", payload)


@pytest.mark.parametrize("method", ["batch_add", "batch_add_async"])
def test_coords_exception_does_not_apply_to_batch_items(method) -> None:
    payload = _write_payload(method, {"coords": {"team": "t"}})
    payload["items"][0]["system_metadata"] = {"coords": {"team": "t"}}

    with pytest.raises(ValidationError, match=r"items\[0\].system_metadata.coords"):
        parse_request(method, payload)


def test_check_write_does_not_gain_coords_routing() -> None:
    with pytest.raises(ValidationError, match="coords"):
        parse_request("check_write", {
            "scope": {"org": "local"}, "system_metadata": {"coords": {"team": "t"}},
        })


def test_update_patch_does_not_gain_coords_routing() -> None:
    with pytest.raises(ValidationError, match="coords"):
        parse_request("update", {
            "unit_id": "u1", "scope": {"org": "local"},
            "patch": {"system_metadata": {"coords": {"team": "t"}}},
        })
