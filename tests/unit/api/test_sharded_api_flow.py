# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09：API 级分片端到端流程（显式分片写入 → 按分片检索 / 列表 → 非法分片）。

现有分片用例多在 storage 层用替身，装配链路的错配（例如默认 IndexBuilder 只写
派生索引、不写正排 KV）在那里看不出来。本用例从 ``MemoryAPI`` 入口走完整默认装配：
写入指定分片、按 ``extensions["shards"]`` 收窄 search/list、非法分片名报错。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control.types import MemoryPatch, UpdateMode
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="alice")


def _sharded_config(
    *,
    vector_enabled: bool = True,
    index_builder: str | None = None,
    relocate_on_update: bool = False,
) -> Config:
    """内存分片部署：KV + 向量三层 + 全文三层都是 shard_routed。

    ``index_builder`` 显式指定 ``constructor.default.target``（缺省是 ``hybrid``，
    它会写正排 KV）；传 ``fulltext`` / ``vector`` 即"只写派生索引"的构建器。
    """

    def routed(table: str) -> dict:
        return {
            "target": "shard_routed",
            "params": {
                "resolver": "default",
                "locator": "default",
                "mode": "split",
                "table_template": {
                    "target": "memory",
                    "params": {"table": f"{table}_{{shard}}"},
                },
            },
        }

    raw: dict = {
        "globals": {
                "vector_enabled": vector_enabled,
                "graph_enabled": False,
                "layers_index_enabled": True,
            },
            "shard_resolver": {
                "default": {
                    "target": "rule_based",
                    "params": {
                        "shards": ["working", "profile"],
                        "default_shard": "working",
                        "rules": [{"shard_from": "system_metadata.shard"}],
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
                            "params": {"table": "kv_{shard}"},
                        },
                        "relocate_on_update": relocate_on_update,
                    },
                },
                "legacy": {"target": "memory"},
            },
            "vector_store": {
                name: routed(f"vec_{name}")
                for name in ("default", "layers_l0", "layers_l1")
            },
        "fulltext_store": {
            name: routed(f"ft_{name}")
            for name in ("default", "layers_l0", "layers_l1")
        },
    }
    if index_builder is not None:
        raw["constructor"] = {"default": {"target": index_builder, "params": {}}}
    return Config.from_dict(raw)


def _security() -> object:
    return internal_context(ScopedAuthenticator(SCOPE))


def _ids(result) -> set[str]:
    return {item.unit_id for item in result.items}


def test_explicit_shard_write_then_narrowed_search_and_list() -> None:
    kernel = build_kernel(config=_sharded_config())
    api = kernel.api
    security = _security()

    profile_units = api.add(
        "交付评审稿放在 profile 分片",
        SCOPE,
        security=security,
        system_metadata={"shard": "profile"},
    )
    working_units = api.add("日常进展放在 working 分片", SCOPE, security=security)
    profile_id = profile_units[0].id
    working_id = working_units[0].id

    profile_hits = api.search(
        "评审稿", Context(scope=SCOPE, extensions={"shards": ["profile"]}), security=security
    )
    assert profile_id in _ids(profile_hits)
    assert working_id not in _ids(profile_hits)

    working_hits = api.search(
        "评审稿", Context(scope=SCOPE, extensions={"shards": ["working"]}), security=security
    )
    assert profile_id not in _ids(working_hits)

    listed_profile = api.list(
        SCOPE, security=security, extensions={"shards": ["profile"]}
    )
    assert {unit.id for unit in listed_profile.items} == {profile_id}
    listed_working = api.list(
        SCOPE, security=security, extensions={"shards": ["working"]}
    )
    assert {unit.id for unit in listed_working.items} == {working_id}

    # 按 id 点读不依赖 shards。
    assert api.get(profile_id, SCOPE, security=security).id == profile_id


def test_invalid_shard_names_are_rejected_at_the_api_boundary() -> None:
    kernel = build_kernel(config=_sharded_config())
    api = kernel.api
    security = _security()
    api.add("任意内容", SCOPE, security=security)

    for invalid in (["ghost"], [], None):
        with pytest.raises(ValidationError):
            api.search(
                "内容",
                Context(scope=SCOPE, extensions={"shards": invalid}),
                security=security,
            )
        with pytest.raises(ValidationError):
            api.list(SCOPE, security=security, extensions={"shards": invalid})


@pytest.mark.parametrize("builder", ["fulltext", "vector"])
def test_sharded_assembly_rejects_builder_without_forward_index(builder: str) -> None:
    """只写派生索引的构建器不写正排 KV，分片归属永远不会创建——装配期直接报错。"""
    with pytest.raises(ValidationError, match="forward"):
        build_kernel(config=_sharded_config(index_builder=builder))


# ---------------------------------------------------------------------------
# F09 D16：改路由字段后的搬迁（端到端）
# ---------------------------------------------------------------------------


def _shard_ids(api, security, shard: str) -> set[str]:
    hits = api.search(
        "交付评审稿", Context(scope=SCOPE, extensions={"shards": [shard]}), security=security
    )
    return _ids(hits)


def test_default_update_creates_new_version_in_new_shard_without_relocation() -> None:
    """默认 SUPERSEDE 生成新版本，新版本按当前内容落位；不需要开启搬迁。"""
    kernel = build_kernel(config=_sharded_config())
    api, security = kernel.api, _security()
    old = api.add("交付评审稿", SCOPE, security=security, system_metadata={"shard": "working"})[0]

    new = api.update(
        old.id, SCOPE, MemoryPatch(system_metadata={"shard": "profile"}), security=security
    )

    assert new.id != old.id
    assert new.id in _shard_ids(api, security, "profile")
    assert old.id not in _shard_ids(api, security, "working"), "superseded version hidden"


def test_overwrite_update_relocates_after_relocate_pending() -> None:
    kernel = build_kernel(config=_sharded_config(relocate_on_update=True))
    api, security = kernel.api, _security()
    memory = api.add(
        "交付评审稿", SCOPE, security=security, system_metadata={"shard": "working"}
    )[0]

    updated = api.update(
        memory.id,
        SCOPE,
        MemoryPatch(system_metadata={"shard": "profile"}, mode=UpdateMode.OVERWRITE),
        security=security,
    )
    assert updated.id == memory.id
    # 搬迁执行前：仍在原分片（读取与按分片检索都以源分片为准）
    assert memory.id in _shard_ids(api, security, "working")
    assert memory.id not in _shard_ids(api, security, "profile")

    report = kernel.storage.kv().relocate_pending(min_age=0)

    assert report.moved == 1, str(report)
    assert memory.id in _shard_ids(api, security, "profile")
    assert memory.id not in _shard_ids(api, security, "working")
    listed = api.list(SCOPE, security=security, extensions={"shards": ["profile"]})
    assert memory.id in {unit.id for unit in listed.items}
    assert api.get(memory.id, SCOPE, security=security).system_metadata["shard"] == "profile"


def test_overwrite_without_relocation_keeps_sticky_placement() -> None:
    kernel = build_kernel(config=_sharded_config())
    api, security = kernel.api, _security()
    memory = api.add(
        "交付评审稿", SCOPE, security=security, system_metadata={"shard": "working"}
    )[0]

    api.update(
        memory.id,
        SCOPE,
        MemoryPatch(system_metadata={"shard": "profile"}, mode=UpdateMode.OVERWRITE),
        security=security,
    )

    assert memory.id in _shard_ids(api, security, "working")
    assert memory.id not in _shard_ids(api, security, "profile")
