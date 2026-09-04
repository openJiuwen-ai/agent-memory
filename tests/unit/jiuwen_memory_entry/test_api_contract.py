# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared HTTP/CLI JSON contract tests aligned with MemoryAPI."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timezone

import pytest

from jiuwen_memory.api import (
    Action,
    BatchWriteItem,
    Context,
    DeleteMode,
    DeleteSelector,
    DisclosureLevel,
    MemoryAPI,
    MemoryPatch,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    SpaceMember,
    SpaceSpec,
    UpdateMode,
    ValidationError,
)
from jiuwen_memory.common.security.space_roles import SpaceContentRole, SpaceGovernanceRole
from jiuwen_memory_entry.core import api_contract
from jiuwen_memory_entry.core.api_contract import invoke_api

pytestmark = pytest.mark.unit


def test_http_method_registry_exactly_matches_memory_api() -> None:
    assert api_contract.api_method_names() == MemoryAPI.__abstractmethods__
    assert len(api_contract.api_method_names()) == 36
    assert api_contract.is_known_verb("add_async") is True
    assert api_contract.is_known_verb("does_not_exist") is False


@pytest.mark.parametrize("verb", sorted(MemoryAPI.__abstractmethods__))
def test_http_request_fields_are_derived_from_api_signature(verb: str) -> None:
    signature = inspect.signature(getattr(MemoryAPI, verb))
    expected = set(signature.parameters) - {"self", "security"}

    assert set(api_contract.method_contract(verb).request_parameters) == expected


def test_add_request_decodes_api_named_fields_and_types() -> None:
    arguments = api_contract.parse_request(
        "add",
        {
            "content": "remember",
            "scope": {"org": "acme", "space": "product", "user": "alice"},
            "source": "document",
            "assets": ["file:///tmp/spec.pdf"],
            "occurred_at": "2026-09-04T09:05:00+08:00",
        },
    )

    assert arguments == {
        "content": "remember",
        "scope": Scope(org="acme", space="product", user="alice"),
        "source": Modality.DOCUMENT,
        "assets": ["file:///tmp/spec.pdf"],
        "occurred_at": datetime.fromisoformat("2026-09-04T09:05:00+08:00"),
    }


def test_omitted_optional_fields_are_left_to_memory_api_defaults() -> None:
    arguments = api_contract.parse_request(
        "add",
        {"content": "remember", "scope": {"org": "acme", "user": "alice"}},
    )

    assert arguments == {
        "content": "remember",
        "scope": Scope(org="acme", user="alice"),
    }


@pytest.mark.parametrize("body", [None, [], "text", 42])
def test_request_body_must_be_json_object(body) -> None:
    with pytest.raises(ValidationError, match="JSON object"):
        api_contract.parse_request("admin_all", body)


@pytest.mark.parametrize("field", ["target", "item_id", "k", "security", "actor_user"])
def test_http_aliases_and_security_fields_are_rejected(field: str) -> None:
    body = {
        "content": "remember",
        "scope": {"org": "acme", "user": "alice"},
        field: "unexpected",
    }

    with pytest.raises(ValidationError):
        api_contract.parse_request("add", body)


def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="content"):
        api_contract.parse_request("add", {"scope": {"org": "acme", "user": "alice"}})


def test_nested_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        api_contract.parse_request("batch_add", {"items": [{}], "scope": {"org": "acme"}})

    assert str(exc.value) == "missing required field(s) for items[0]: 'content'"


def test_batch_request_decodes_nested_batch_items() -> None:
    arguments = api_contract.parse_request(
        "batch_add",
        {
            "items": [
                {
                    "content": "one",
                    "scope": {"org": "acme", "user": "alice"},
                    "source": "code",
                    "sequence": 1,
                }
            ],
            "scope": {"org": "acme", "user": "default"},
        },
    )

    assert arguments["scope"] == Scope(org="acme", user="default")
    assert arguments["items"] == [
        BatchWriteItem(
            content="one",
            scope=Scope(org="acme", user="alice"),
            source=Modality.CODE,
            sequence=1,
        )
    ]


def test_update_request_decodes_complete_memory_patch() -> None:
    arguments = api_contract.parse_request(
        "update",
        {
            "unit_id": "unit-1",
            "scope": {"org": "acme", "user": "alice"},
            "patch": {
                "content": "updated",
                "tier": "semantic",
                "t_valid": "2026-09-04T00:00:00Z",
                "mode": "overwrite",
            },
        },
    )

    assert arguments["patch"] == MemoryPatch(
        content="updated",
        tier=MemoryTier.SEMANTIC,
        t_valid=datetime(2026, 9, 4, tzinfo=timezone.utc),
        mode=UpdateMode.OVERWRITE,
    )


def test_delete_request_decodes_selector_without_flattening() -> None:
    arguments = api_contract.parse_request(
        "delete",
        {
            "selector": {
                "unit_ids": ["unit-1"],
                "scope": {"org": "acme", "user": "alice"},
                "mode": "purge",
            }
        },
    )

    assert arguments["selector"] == DeleteSelector(
        unit_ids=["unit-1"],
        scope=Scope(org="acme", user="alice"),
        mode=DeleteMode.PURGE,
    )


def test_search_request_decodes_context_and_keeps_dict_filter_dsl() -> None:
    arguments = api_contract.parse_request(
        "search",
        {
            "query": "coffee",
            "context": {
                "scope": {"org": "acme", "user": "alice"},
                "extensions": {"language": "zh"},
            },
            "filters": {"tags": {"contains": "habit"}},
            "disclosure": "l2",
        },
    )

    assert arguments["context"] == Context(
        scope=Scope(org="acme", user="alice"), extensions={"language": "zh"}
    )
    assert arguments["filters"] == {"tags": {"contains": "habit"}}
    assert arguments["disclosure"] is DisclosureLevel.L2


def test_grant_request_decodes_nested_scopes_actions_and_datetime() -> None:
    arguments = api_contract.parse_request(
        "grant",
        {
            "grant": {
                "grantor": {"org": "acme", "user": "owner"},
                "grantee": {"org": "acme", "user": "reader"},
                "actions": ["read", "write"],
                "expires_at": "2026-10-01T00:00:00Z",
            }
        },
    )

    grant = arguments["grant"]
    assert grant.grantor == Scope(org="acme", user="owner")
    assert grant.grantee == Scope(org="acme", user="reader")
    assert grant.actions == frozenset({Action.READ, Action.WRITE})
    assert grant.expires_at == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_space_request_decodes_domain_objects_with_their_defaults() -> None:
    spec = api_contract.parse_request(
        "create_space",
        {"spec": {"org": "acme", "space": "product", "display_name": "Product"}},
    )["spec"]
    member = api_contract.parse_request(
        "add_space_member",
        {
            "org": "acme",
            "space": "product",
            "member": {
                "scope": {"org": "acme", "user": "alice"},
                "content_role": "viewer",
                "governance_role": "manager",
            },
        },
    )["member"]

    assert spec == SpaceSpec(org="acme", space="product", display_name="Product")
    assert member == SpaceMember(
        scope=Scope(org="acme", user="alice"),
        content_role=SpaceContentRole.VIEWER,
        governance_role=SpaceGovernanceRole.MANAGER,
    )


def test_nested_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown field"):
        api_contract.parse_request(
            "update",
            {
                "unit_id": "unit-1",
                "scope": {"org": "acme", "user": "alice"},
                "patch": {"hard": True},
            },
        )


def test_json_serializer_preserves_dataclass_fields_and_wire_values() -> None:
    unit = MemoryUnit(
        id="unit-1",
        scope=Scope(org="acme", user="alice"),
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="coffee", source=Modality.TEXT)],
    )

    result = api_contract.to_jsonable([unit])

    assert result[0]["id"] == "unit-1"
    assert result[0]["scope"] == {
        "org": "acme",
        "space": "",
        "user": "alice",
        "agent": "",
        "session": "",
    }
    assert result[0]["tier"] == "semantic"
    assert result[0]["segments"] == [{"content": "coffee", "assets": [], "source": "text"}]
    assert "item_id" not in result[0]


def test_sync_and_async_http_invocation_use_same_named_api_methods() -> None:
    calls: list[str] = []

    class _Api:
        @staticmethod
        def add(content, scope, source=Modality.TEXT, *, security, **_kwargs):
            del content, source, security
            calls.append("add")
            return [MemoryUnit(id="sync", scope=scope)]

        @staticmethod
        async def add_async(content, scope, source=Modality.TEXT, *, security, **_kwargs):
            del content, source, security
            calls.append("add_async")
            return [MemoryUnit(id="async", scope=scope)]

    raw = {"content": "remember", "scope": {"org": "acme", "user": "alice"}}

    sync_result = invoke_api(_Api(), "add", raw, object())
    async_result = invoke_api(_Api(), "add_async", raw, object())

    assert calls == ["add", "add_async"]
    assert sync_result[0]["id"] == "sync"
    assert async_result[0]["id"] == "async"
    assert not any(dataclasses.is_dataclass(result) for result in (sync_result, async_result))
