# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP-03 DTO contract tests."""

from __future__ import annotations

import importlib
import os
import sys

import pytest

pytestmark = pytest.mark.unit

_HTTP = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "jiuwen_memory_entry", "http_server"
    )
)
_CLI = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "jiuwen_memory_entry", "cli"
    )
)
if _HTTP not in sys.path:
    sys.path.append(_HTTP)
if _CLI not in sys.path:
    sys.path.append(_CLI)

dto = importlib.import_module("dto")
client = importlib.import_module("client")


def test_target_fields_map_to_handler_and_domain_scope() -> None:
    request = dto.parse_request(
        "add",
        {
            "target": {
                "tenant_id": "acme",
                "space": "product",
                "scope": "alice",
                "agent": "agent-a",
                "session": "session-1",
            },
            "content": "remember",
        },
    )

    assert request.target.to_scope() == dto.Scope(
        org="acme", space="product", user="alice", agent="agent-a", session="session-1"
    )
    assert "tenant_id" not in request.payload
    assert "scope" not in request.payload


def test_dispatch_request_keeps_scope_out_of_business_payload() -> None:
    request = dto.parse_request(
        "add", {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "remember"}
    )
    dispatch_request = request.to_dispatch_request(actor=dto.Scope(org="acme", user="writer"))

    assert dispatch_request.target == dto.Scope(org="acme", user="alice")
    assert dispatch_request.actor == dto.Scope(org="acme", user="writer")
    assert dict(dispatch_request.payload) == {"content": "remember"}


@pytest.mark.parametrize("body", [None, [], "text", 42])
def test_request_body_must_be_object(body) -> None:
    with pytest.raises(dto.ValidationError, match="JSON object"):
        dto.parse_request("add", body)


def test_reserved_identity_field_is_rejected() -> None:
    with pytest.raises(dto.ValidationError, match="reserved"):
        dto.parse_request(
            "add",
            {"target": {"tenant_id": "acme"}, "content": "x", "actor_scope": "root"},
        )


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(dto.ValidationError, match="unknown field"):
        dto.parse_request("add", {"target": {"tenant_id": "acme"}, "contnet": "x"})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("content", 42, "string"), ("tags", "tag", "array")],
)
def test_business_field_types_are_checked(field: str, value, message: str) -> None:
    with pytest.raises(dto.ValidationError, match=message):
        dto.parse_request(
            "add",
            {"target": {"tenant_id": "acme"}, "content": "x", field: value},
        )


def test_boolean_field_types_are_checked() -> None:
    with pytest.raises(dto.ValidationError, match="boolean"):
        dto.parse_request(
            "delete",
            {"target": {"tenant_id": "acme", "scope": "alice"}, "item_id": "id", "hard": "true"},
        )


def test_add_accepts_video_uri_and_canonical_source() -> None:
    request = dto.parse_request(
        "add",
        {
            "target": {"tenant_id": "acme", "scope": "alice"},
            "content": "video upload",
            "source": "video",
            "uri": "file:///tmp/demo.mp4",
        },
    )

    assert request.payload == {
        "content": "video upload",
        "source": "video",
        "uri": "file:///tmp/demo.mp4",
    }


def test_add_rejects_conflicting_source_and_modality() -> None:
    with pytest.raises(dto.ValidationError, match="source and modality"):
        dto.parse_request(
            "add",
            {
                "target": {"tenant_id": "acme", "scope": "alice"},
                "content": "x",
                "source": "image",
                "modality": "video",
            },
        )


@pytest.mark.parametrize("location", ["defaults", "items"])
def test_batch_add_rejects_video_uri_without_a_single_add_handler(location: str) -> None:
    body = {"target": {"tenant_id": "acme"}, "items": [{"content": "x"}]}
    if location == "defaults":
        body["defaults"] = {"uri": "file:///tmp/demo.mp4"}
    else:
        body["items"][0]["uri"] = "file:///tmp/demo.mp4"

    with pytest.raises(dto.ValidationError, match="uri"):
        dto.parse_request("batch_add", body)


def test_delete_space_uses_delete_mode_wire_field() -> None:
    request = dto.parse_request(
        "delete_space",
        {"target": {"tenant_id": "acme", "space": "product"}, "mode": "purge"},
    )

    assert request.payload == {"mode": "purge"}


def test_delete_space_rejects_unsupported_confirmation_fields() -> None:
    with pytest.raises(dto.ValidationError, match="unknown field"):
        dto.parse_request(
            "delete_space",
            {
                "target": {"tenant_id": "acme", "space": "product"},
                "hard": True,
            },
        )


@pytest.mark.parametrize(
    ("verb", "field", "value", "message"),
    [
        ("add", "modality", "not-a-modality", "modality must be one of"),
        ("evolve", "mode", "not-a-mode", "mode must be one of"),
        ("list_spaces", "status", "not-a-status", "status must be one of"),
    ],
)
def test_enum_values_are_checked_at_dto_boundary(
    verb: str, field: str, value: str, message: str
) -> None:
    body = {"target": {"tenant_id": "acme"}, field: value}
    if verb == "evolve":
        body["target"] = {"tenant_id": "acme", "scope": "alice"}
    with pytest.raises(dto.ValidationError, match=message):
        dto.parse_request(verb, body)


def test_array_elements_and_numeric_ranges_are_checked() -> None:
    with pytest.raises(dto.ValidationError, match="non-empty strings"):
        dto.parse_request(
            "add", {"target": {"tenant_id": "acme"}, "content": "x", "tags": ["ok", ""]}
        )
    with pytest.raises(dto.ValidationError, match="positive integer"):
        dto.parse_request("search", {"target": {"tenant_id": "acme"}, "query": "x", "k": 0})


def test_batch_item_accepts_item_ordering_fields() -> None:
    request = dto.parse_request(
        "batch_add",
        {
            "target": {"tenant_id": "acme"},
            "items": [{"content": "x", "sequence": 1, "idempotency_key": "item-1"}],
        },
    )

    assert request.items[0].payload["sequence"] == 1
    assert request.items[0].payload["idempotency_key"] == "item-1"


def test_export_space_include_audit_is_allowed() -> None:
    request = dto.parse_request(
        "export_space",
        {"target": {"tenant_id": "acme", "space": "product"}, "include_audit": False},
    )

    assert request.payload["include_audit"] is False


def test_space_operations_reject_user_dimensions_on_space_target() -> None:
    with pytest.raises(dto.ValidationError, match="tenant_id and space only"):
        dto.parse_request(
            "get_space",
            {"target": {"tenant_id": "acme", "space": "product", "scope": "alice"}},
        )


def test_space_and_space_id_conflict_is_rejected() -> None:
    with pytest.raises(dto.ValidationError, match="space.*space_id"):
        dto.parse_request(
            "list",
            {"target": {"tenant_id": "acme", "space": "a", "space_id": "b"}},
        )


def test_batch_item_target_is_explicit_and_legacy_target_scope_is_internal_only() -> None:
    request = dto.parse_request(
        "batch_add",
        {
            "target": {"tenant_id": "acme", "scope": "default"},
            "items": [
                {"content": "one"},
                {"content": "two", "target": {"tenant_id": "acme", "scope": "alice"}},
            ],
        },
    )

    assert request.items[0].payload == {"content": "one"}
    assert request.items[1].target.scope == "alice"


def test_grantee_scope_maps_to_existing_handler_shape() -> None:
    request = dto.parse_request(
        "grant",
        {
            "target": {"tenant_id": "acme", "scope": "owner"},
            "grantee": {"tenant_id": "acme", "scope": "reader"},
        },
    )

    assert request.grantee.to_scope() == dto.Scope(org="acme", user="reader")


def test_structured_scope_fields_are_not_duplicated_in_dispatch_payload() -> None:
    actor = dto.Scope(org="acme", user="writer")
    grant = dto.parse_request(
        "grant",
        {
            "target": {"tenant_id": "acme", "scope": "owner"},
            "grantee": {"tenant_id": "acme", "scope": "reader"},
        },
    ).to_dispatch_request(actor=actor)
    member = dto.parse_request(
        "add_space_member",
        {
            "target": {"tenant_id": "acme", "space": "product"},
            "member": {"tenant_id": "acme", "scope": "reader"},
            "role": "viewer",
        },
    ).to_dispatch_request(actor=actor)
    batch = dto.parse_request(
        "batch_add",
        {
            "target": {"tenant_id": "acme", "scope": "owner"},
            "items": [{"content": "remember"}],
        },
    ).to_dispatch_request(actor=actor)

    assert dict(grant.payload) == {}
    assert grant.grantee == dto.Scope(org="acme", user="reader")
    assert dict(member.payload) == {"role": "viewer"}
    assert member.member == dto.Scope(org="acme", user="reader")
    assert dict(batch.payload) == {}
    assert [dict(item.payload) for item in batch.batch_items] == [{"content": "remember"}]


def test_http_client_wraps_legacy_flat_scope_fields() -> None:
    assert client.to_http_payload(
        "add", {"tenant_id": "acme", "scope": "alice", "content": "x"}
    ) == {
        "target": {"tenant_id": "acme", "scope": "alice"},
        "content": "x",
    }


def test_http_client_reads_api_key_without_putting_it_in_payload(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_API_KEY", "secret-test-key")
    http_client = client.HttpClient("http://127.0.0.1:1")

    assert http_client.api_key == "secret-test-key"
    assert "secret-test-key" not in client.to_http_payload(
        "add", {"tenant_id": "acme", "scope": "alice", "content": "x"}
    )


def test_http_client_serializes_typed_grantee_and_batch_scope_from_legacy_adapter() -> None:
    grant = client.to_http_payload(
        "grant",
        {
            "tenant_id": "acme",
            "scope": "owner",
            "grantee_tenant_id": "acme",
            "grantee_scope": "reader",
        },
    )
    batch = client.to_http_payload(
        "batch_add",
        {
            "defaults": {"tenant_id": "acme", "scope": "owner"},
            "items": [
                {"content": "one"},
                {"content": "two", "target_scope": {"scope": "reader"}},
            ],
        },
    )

    assert grant == {
        "target": {"tenant_id": "acme", "scope": "owner"},
        "grantee": {"tenant_id": "acme", "scope": "reader"},
    }
    assert batch == {
        "target": {"tenant_id": "acme", "scope": "owner"},
        "defaults": {},
        "items": [
            {"content": "one"},
            {"content": "two", "target": {"tenant_id": "acme", "scope": "reader"}},
        ],
    }


def test_known_verb_registry_is_explicit() -> None:
    assert dto.is_known_verb("add") is True
    assert dto.is_known_verb("does_not_exist") is False


@pytest.mark.parametrize("verb", ["add", "search", "get", "update", "delete", "list"])
def test_data_verbs_require_target(verb: str) -> None:
    with pytest.raises(dto.ValidationError, match="target"):
        dto.parse_request(verb, {})


@pytest.mark.parametrize("verb", ["audit", "admin"])
def test_audit_and_admin_may_omit_target(verb: str) -> None:
    body = {"filters": {}} if verb == "audit" else {"key": "health", "value": "ok"}
    assert dto.parse_request(verb, body).target is None


@pytest.mark.parametrize("target", [None, [], "scope", 1])
def test_target_must_be_an_object(target) -> None:
    with pytest.raises(dto.ValidationError, match="target"):
        dto.parse_request("add", {"target": target, "content": "x"})


@pytest.mark.parametrize(
    "field",
    [
        "actor_tenant_id",
        "actor_space",
        "actor_space_id",
        "actor_scope",
        "actor_agent",
        "actor_session",
        "identity",
        "role",
        "acting_user",
        "principal",
        "authenticated_user",
    ],
)
@pytest.mark.parametrize("location", ["top", "target", "grantee", "member"])
def test_reserved_identity_fields_are_rejected_at_every_nested_boundary(
    field: str, location: str
) -> None:
    body = {"target": {"tenant_id": "acme", "scope": "owner"}}
    if location == "top":
        body[field] = "root"
        verb = "add"
    elif location == "target":
        body["target"][field] = "root"
        verb = "add"
    else:
        verb = "grant" if location == "grantee" else "add_space_member"
        nested = {"tenant_id": "acme", "scope": "reader", field: "root"}
        body[location] = nested
        if location == "member":
            body["target"] = {"tenant_id": "acme", "space": "product"}
    with pytest.raises(dto.ValidationError, match="reserved"):
        dto.parse_request(verb, body)


@pytest.mark.parametrize("nested", ["items", "defaults"])
def test_reserved_identity_fields_are_rejected_in_batch_nested_payloads(nested: str) -> None:
    body = {
        "target": {"tenant_id": "acme"},
        "items": [{"content": "x"}],
    }
    if nested == "items":
        body["items"][0]["actor_scope"] = "root"
    else:
        body["defaults"] = {"actor_scope": "root"}
    with pytest.raises(dto.ValidationError, match="reserved"):
        dto.parse_request("batch_add", body)


def test_target_unknown_field_is_rejected() -> None:
    with pytest.raises(dto.ValidationError, match="unknown target field"):
        dto.parse_request("add", {"target": {"tenant_id": "acme", "org": "acme"}})


def test_member_role_is_business_field_but_other_reserved_names_are_not() -> None:
    request = dto.parse_request(
        "add_space_member",
        {
            "target": {"tenant_id": "acme", "space": "product"},
            "member": {"tenant_id": "acme", "scope": "reader"},
            "role": "viewer",
        },
    )
    assert request.payload["role"] == "viewer"


@pytest.mark.parametrize("field", ["tenant_id", "scope", "space", "agent", "session"])
def test_target_scope_values_must_be_non_empty_strings(field: str) -> None:
    target = {"tenant_id": "acme"}
    target[field] = "   " if field != "tenant_id" else ""
    with pytest.raises(dto.ValidationError, match="non-empty string"):
        dto.parse_request("add", {"target": target, "content": "x"})


def test_space_id_alias_maps_to_canonical_space() -> None:
    request = dto.parse_request(
        "get_space", {"target": {"tenant_id": "acme", "space_id": "product"}}
    )
    assert request.target.space == "product"
    assert "space" not in request.payload


@pytest.mark.parametrize(
    "verb", ["create_space", "get_space", "update_space", "archive_space", "delete_space"]
)
def test_space_verbs_require_only_org_and_space(verb: str) -> None:
    with pytest.raises(dto.ValidationError, match="target.space"):
        dto.parse_request(verb, {"target": {"tenant_id": "acme"}})
    with pytest.raises(dto.ValidationError, match="tenant_id and space only"):
        dto.parse_request(verb, {"target": {"tenant_id": "acme", "space": "p", "agent": "a"}})


def test_list_spaces_accepts_tenant_only() -> None:
    request = dto.parse_request("list_spaces", {"target": {"tenant_id": "acme"}})
    assert request.target.space == ""


@pytest.mark.parametrize(
    "nested,verb,field", [("grantee", "grant", "grantee"), ("member", "add_space_member", "member")]
)
def test_nested_scope_requires_scope(nested: str, verb: str, field: str) -> None:
    target = {"tenant_id": "acme", "scope": "owner"}
    if field == "member":
        target = {"tenant_id": "acme", "space": "product"}
    with pytest.raises(dto.ValidationError, match="scope"):
        dto.parse_request(verb, {"target": target, nested: {"tenant_id": "acme"}})


@pytest.mark.parametrize("items", [None, [], {}, ["not-an-object"]])
def test_batch_items_are_non_empty_array_of_objects(items) -> None:
    with pytest.raises(dto.ValidationError, match="(items|batch item)"):
        dto.parse_request("batch_add", {"target": {"tenant_id": "acme"}, "items": items})


def test_batch_defaults_are_strictly_parsed() -> None:
    with pytest.raises(dto.ValidationError, match="defaults"):
        dto.parse_request(
            "batch_add",
            {"target": {"tenant_id": "acme"}, "items": [{"content": "x"}], "defaults": []},
        )
    with pytest.raises(dto.ValidationError, match="unknown field"):
        dto.parse_request(
            "batch_add",
            {
                "target": {"tenant_id": "acme"},
                "items": [{"content": "x"}],
                "defaults": {"unknown": 1},
            },
        )


def test_memory_type_is_validated_as_a_string() -> None:
    with pytest.raises(dto.ValidationError, match="memory_type must be a string"):
        dto.parse_request("list", {"target": {"tenant_id": "acme"}, "memory_type": 7})


@pytest.mark.parametrize("field", ["k", "limit", "max_tokens", "offset", "sequence"])
def test_integer_fields_reject_boolean(field: str) -> None:
    verb = (
        "search"
        if field in {"k", "max_tokens"}
        else "list"
        if field in {"limit", "offset"}
        else "batch_add"
    )
    extra = {"items": [{"content": "x", "sequence": True}]} if field == "sequence" else {}
    if field == "sequence":
        with pytest.raises(dto.ValidationError, match="integer"):
            dto.parse_request(verb, {"target": {"tenant_id": "acme"}, **extra})
        return
    with pytest.raises(dto.ValidationError, match="integer"):
        dto.parse_request(verb, {"target": {"tenant_id": "acme"}, field: True})
