"""角色记忆配置 + 分片 KV：运行时应能完成装配并共享分片数据面。"""

from __future__ import annotations

import pytest

from jiuwen_memory.api import (
    Context,
    MemoryRef,
    RelationRecord,
    TaskRequest,
)
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import MemoryTier, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import MemoryPatch, SpaceSpec, UpdateMode
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def test_role_memory_runtime_assembles_with_sharded_kv() -> None:
    config = Config.from_dict(
        {
            "engine": {
                "default": {
                    "target": "cloud",
                    "params": dict.fromkeys(
                        (
                            "ingestor",
                            "retriever",
                            "evolver",
                            "index_builder",
                            "classifier",
                            "scheduler",
                            "policy",
                        ),
                        "default",
                    ),
                }
            },
            "authorizer": {
                "default": {"target": "space_aware", "params": {"delegate": "standard"}},
                "standard": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                },
            },
            "continuity": {
                "default": {
                    "target": "local",
                    "params": {"single_writer": True, "allow_ephemeral": True, "mode": "role"},
                }
            },
            "role_view": {"default": {"target": "bounded"}},
            "shard_resolver": {
                "default": {
                    "target": "rule_based",
                    "params": {
                        "shards": ["working", "profile"],
                        "default_shard": "working",
                        "rules": [{"shard": "profile", "match": {"tier": "core"}}],
                    },
                }
            },
            "kv_store": {
                "default": {
                    "target": "shard_routed",
                    "params": {
                        "resolver": "default",
                        "passthrough": "legacy",
                        "placement_store": {"target": "memory"},
                        "table_template": {
                            "target": "memory",
                            "params": {"table": "agent_memory_kv_{shard}"},
                        },
                        "relocate_on_update": True,
                    },
                },
                "legacy": {"target": "memory"},
            },
        }
    )
    runtime = _build_kernel(config=config)
    try:
        api = runtime.api
        project = Scope(org="acme", space="project")
        owner = Scope(org="acme", user="owner")
        security = internal_context(ScopedAuthenticator(owner))
        admin = internal_context(ScopedAuthenticator(owner, role=Role.ADMIN))
        api.create_space(SpaceSpec(org="acme", space="project", owner=owner), security=admin)
        api.continuity_task(
            project, TaskRequest("create", "create"), security=security
        )
        a = api.add("payment timeout", project, security=security)[0]
        b = api.add("a test method", project, security=security)[0]
        relation = RelationRecord(
            "r",
            MemoryRef.of(b),
            MemoryRef.of(a),
            "verification_method_for",
            status="confirmed",
            evidence_refs=[MemoryRef.of(b)],
        )
        api.continuity_relation(project, relation, 0, security=security)
        view = api.search(
            "",
            Context(project),
            security=security,
            anchor=MemoryRef.of(a),
            duty="testing",
        )
        assert any(p["steps"][-1]["relation_id"] == "r" for p in view.paths)

        # D16：原地改 tier 触发搬迁；证据图按 scope + unit_id + digest 引用，搬迁后仍有效。
        api.update(
            a.id,
            project,
            MemoryPatch(tier=MemoryTier.CORE, mode=UpdateMode.OVERWRITE),
            security=security,
        )
        report = runtime.storage.kv().relocate_pending(min_age=0)
        assert report.moved == 1
        view_after = api.search(
            "",
            Context(project),
            security=security,
            anchor=MemoryRef.of(a),
            duty="testing",
        )
        assert any(p["steps"][-1]["relation_id"] == "r" for p in view_after.paths)
    finally:
        runtime.close()
