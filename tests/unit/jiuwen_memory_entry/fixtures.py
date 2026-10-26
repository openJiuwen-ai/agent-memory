# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic API failures and real DEV management regression fixtures."""

from typing import Any

import httpx
import openai

from jiuwen_memory.api import MemoryAPI, Scope, SpaceSpec, internal_context
from jiuwen_memory.common.security.types import Role
from tests.support.scoped_authenticator import ScopedAuthenticator

# API/CLI 同名；MCP 加 memory_ 前缀。根审计资源要求 ROOT，ADMIN 并不足够。
DEV_MANAGEMENT_CASES = (
    ("audit", {"filters": {}}, frozenset({"root"})),
    ("verify_audit", {}, frozenset({"root"})),
    ("admin_get", {"key": "rerank.enabled"}, frozenset({"root"})),
    ("admin_set", {"key": "rerank.enabled", "value": "false"}, frozenset({"root"})),
    ("admin_all", {}, frozenset({"root"})),
    (
        "create_space",
        {"spec": {"org": "local", "space": "new-local"}},
        frozenset({"admin", "root"}),
    ),
    ("create_space", {"spec": {"org": "other", "space": "new-other"}}, frozenset({"root"})),
    ("get_space_policy", {"org": "local", "space": "managed"}, frozenset({"root"})),
    ("set_space_policy", {"org": "local", "space": "managed", "policy": {}}, frozenset({"root"})),
    (
        "add_space_member",
        {
            "org": "local",
            "space": "managed",
            "member": {"scope": {"org": "local", "user": "guest"}},
        },
        frozenset({"root"}),
    ),
    (
        "remove_space_member",
        {"org": "local", "space": "managed", "member": {"org": "local", "user": "guest"}},
        frozenset({"root"}),
    ),
)


def fixed_dev_management_config(role: str | None) -> dict[str, Any]:
    """装配真实空间治理链；None 刻意省略 role，验证默认 USER 而非显式 user。"""
    memory_api: dict[str, Any] = {
        "authorizer": {
            "default": {"target": "space_aware", "params": {"delegate": "standard"}},
            "standard": {
                "target": "standard",
                "params": {"grant_store": "default", "delegation_store": "default"},
            },
        }
    }
    if role is not None:
        memory_api["security"] = {
            "default": {
                "target": "standard",
                "params": {"authenticator": {"target": "dev", "params": {"role": role}}},
            }
        }
    return {"memory_api": memory_api}


def seed_dev_management_space(api: MemoryAPI, *, owner: Scope | None = None) -> None:
    """经真实 PEP 由显式测试 ROOT 建立他人空间，不替换被测身份、PDP 或存储。"""
    security = internal_context(ScopedAuthenticator(Scope(org="local", user="ops"), role=Role.ROOT))
    api.create_space(SpaceSpec(org="local", space="managed", owner=owner), security=security)


def upstream_api_error(status: int = 429) -> openai.APIStatusError:
    error_types = {
        429: openai.RateLimitError,
        400: openai.BadRequestError,
        500: openai.InternalServerError,
    }
    error_type = error_types.get(status)
    if error_type is None:
        raise ValueError(f"unsupported upstream status: {status}")
    return error_type(
        "provider-private-detail Authorization: Bearer secret-token api_key=secret-key",
        response=httpx.Response(
            status,
            request=httpx.Request("POST", "https://provider.invalid/v1/embeddings"),
        ),
        body={"error": {"message": "provider-private-detail"}},
    )
