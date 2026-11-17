# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S1：分片规则（ShardResolver）与定位结果契约测试。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Scope, Segment
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.shard_routing import (
    RuleBasedShardResolver,
    ShardLocation,
    ShardResolver,
    ShardResolverProducer,
    validate_shard_name,
)

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


def unit(
    uid: str = "u-1",
    *,
    tier: MemoryTier = MemoryTier.SEMANTIC,
    tags: list[str] | None = None,
    system_metadata: dict | None = None,
    user_metadata: dict | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=SCOPE,
        tier=tier,
        tags=list(tags or []),
        system_metadata=dict(system_metadata or {}),
        user_metadata=dict(user_metadata or {}),
        segments=[Segment(content="x")],
    )


def resolver(**overrides) -> RuleBasedShardResolver:
    params = {
        "shards": ["working", "profile", "episodic", "main"],
        "default_shard": "main",
        "rules": [
            {"shard": "working", "match": {"tier": "working"}},
            {"shard": "profile", "match": {"tier": "core"}},
            {"shard": "episodic", "match": {"tier": "episodic"}},
        ],
    }
    params.update(overrides)
    return RuleBasedShardResolver(**params)


class TestRoutingFields:
    @staticmethod
    def test_rule_matches_tier() -> None:
        """TC-S1-1：match 命中 tier。"""
        assert resolver().resolve(unit(tier=MemoryTier.CORE)) == "profile"

    @staticmethod
    def test_missing_field_falls_back_to_default() -> None:
        """TC-S1-2：字段缺失 / None / 空串视为未命中。"""
        assert resolver().resolve(unit(tier=MemoryTier.SEMANTIC)) == "main"
        r = resolver(
            rules=[{"shard": "working", "match": {"user_metadata.tag": {"contains": "vip"}}}]
        )
        assert r.resolve(unit()) == "main"

    @staticmethod
    def test_first_matching_rule_wins() -> None:
        """TC-S1-3：按顺序首条命中。"""
        r = resolver(
            rules=[
                {"shard": "working", "match": {"tier": "core"}},
                {"shard": "profile", "match": {"tier": "core"}},
            ]
        )
        assert r.resolve(unit(tier=MemoryTier.CORE)) == "working"

    @staticmethod
    def test_shard_from_explicit_value() -> None:
        """TC-S1-4：shard_from 直接取值。"""
        r = resolver(
            rules=[
                {"shard_from": "system_metadata.shard"},
                {"shard": "profile", "match": {"tier": "core"}},
            ]
        )
        assert r.resolve(unit(system_metadata={"shard": "working"})) == "working"

    @staticmethod
    def test_shard_from_unknown_reject() -> None:
        """TC-S1-5：未配置分片名，on_unknown=reject 抛 ValidationError。"""
        r = resolver(rules=[{"shard_from": "system_metadata.shard"}])
        with pytest.raises(ValidationError):
            r.resolve(unit(system_metadata={"shard": "nope"}))

    @staticmethod
    def test_shard_from_unknown_default() -> None:
        """TC-S1-6：on_unknown=default 落 default_shard。"""
        r = resolver(
            rules=[{"shard_from": "system_metadata.shard"}], on_unknown="default"
        )
        assert r.resolve(unit(system_metadata={"shard": "nope"})) == "main"

    @staticmethod
    def test_shard_from_non_string_uses_on_unknown() -> None:
        """shard_from 拿到非字符串值按 on_unknown 处理。"""
        r = resolver(rules=[{"shard_from": "system_metadata.shard"}])
        with pytest.raises(ValidationError):
            r.resolve(unit(system_metadata={"shard": 3}))

    @staticmethod
    def test_shard_from_array_field_rejected() -> None:
        """TC-S1-7：shard_from 不允许指向数组字段。"""
        with pytest.raises(ValidationError):
            resolver(rules=[{"shard_from": "tags"}])

    @staticmethod
    @pytest.mark.parametrize(
        "field",
        [
            "lifecycle",
            "t_valid",
            "t_invalid",
            "t_ingest",
            "t_event",
            "t_message",
            "hierarchy_kind",
            "hierarchy_role",
            "hierarchy_status",
            "tree_parent_id",
            "tree_span_start",
            "tree_span_end",
            "content_layer",
            "seq",
        ],
    )
    def test_denied_routing_field_rejected(field: str) -> None:
        """TC-S1-8：禁用路由字段在启动校验期拒绝。"""
        with pytest.raises(ValidationError):
            resolver(rules=[{"shard": "profile", "match": {field: "x"}}])

    @staticmethod
    def test_identifier_field_rejected() -> None:
        """未在允许集合内的标识字段拒绝。"""
        with pytest.raises(ValidationError):
            resolver(rules=[{"shard": "profile", "match": {"id": "u-1"}}])

    @staticmethod
    def test_kernel_managed_metadata_rejected() -> None:
        """system_metadata 里内核会改写的键不能作为路由字段。"""
        for key in ("middle", "infer", "procedural"):
            with pytest.raises(ValidationError):
                resolver(
                    rules=[{"shard": "profile", "match": {f"system_metadata.{key}": "x"}}]
                )

    @staticmethod
    def test_metadata_nested_path_allowed() -> None:
        """system_metadata.* / user_metadata.* 扁平路径允许。"""
        r = resolver(
            rules=[{"shard": "profile", "match": {"system_metadata.domain": "life"}}]
        )
        assert r.resolve(unit(system_metadata={"domain": "life"})) == "profile"

    @staticmethod
    def test_rule_shard_must_be_configured() -> None:
        """TC-S1-9：规则引用的分片名必须属于 shards。"""
        with pytest.raises(ValidationError):
            resolver(rules=[{"shard": "ghost", "match": {"tier": "core"}}])

    @staticmethod
    def test_default_shard_must_be_configured() -> None:
        """default_shard 必须在 shards 中。"""
        with pytest.raises(ValidationError):
            resolver(default_shard="ghost")

    @staticmethod
    @pytest.mark.parametrize("name", ["Working", "1abc", "a-b", "", "a" * 33])
    def test_invalid_shard_name_rejected(name: str) -> None:
        """TC-S1-10：分片名格式校验。"""
        with pytest.raises(ValidationError):
            validate_shard_name(name)

    @staticmethod
    def test_valid_shard_name() -> None:
        validate_shard_name("working_2")
        validate_shard_name("a")

    @staticmethod
    def test_in_operator_semantics_match_filter() -> None:
        """TC-S1-11：IN 与 filtering 语义一致。"""
        r = resolver(
            rules=[{"shard": "profile", "match": {"tier": {"in": ["core", "semantic"]}}}]
        )
        assert r.resolve(unit(tier=MemoryTier.SEMANTIC)) == "profile"

    @staticmethod
    def test_contains_operator_on_tags() -> None:
        """数组字段用 contains 条件。"""
        r = resolver(rules=[{"shard": "profile", "match": {"tags": {"contains": "vip"}}}])
        assert r.resolve(unit(tags=["vip"])) == "profile"
        assert r.resolve(unit(tags=["other"])) == "main"

    @staticmethod
    def test_enum_value_comparison() -> None:
        """TC-S1-12：枚举按其 value 字符串比较。"""
        r = resolver(rules=[{"shard": "profile", "match": {"tier": "core"}}])
        assert r.resolve(unit(tier=MemoryTier.CORE)) == "profile"

    @staticmethod
    def test_empty_string_shard_from_continues() -> None:
        """显式分片键为空串时继续后续规则。"""
        r = resolver(
            rules=[
                {"shard_from": "system_metadata.shard"},
                {"shard": "profile", "match": {"tier": "core"}},
            ]
        )
        assert r.resolve(unit(tier=MemoryTier.CORE)) == "profile"

    @staticmethod
    def test_invalid_rule_shape_rejected() -> None:
        """一条规则只能二选一：match 或 shard_from。"""
        with pytest.raises(ValidationError):
            resolver(
                rules=[
                    {"shard": "profile", "match": {"tier": "core"}, "shard_from": "tier"}
                ]
            )
        with pytest.raises(ValidationError):
            resolver(rules=[{"shard": "profile"}])

    @staticmethod
    def test_invalid_on_unknown_rejected() -> None:
        with pytest.raises(ValidationError):
            resolver(on_unknown="ignore")


class TestProducerAndContracts:
    @staticmethod
    def test_rule_based_registered() -> None:
        """rule_based target 自注册且实现 ShardResolver 契约。"""
        assert isinstance(getattr(ShardResolverProducer, "_registry")["rule_based"], object)
        built = ShardResolverProducer.build(
            "rule_based",
            {"shards": ["a"], "default_shard": "a", "rules": []},
            AssemblyContext(),
        )
        assert isinstance(built, ShardResolver)
        assert built.resolve(unit()) == "a"

    @staticmethod
    def test_location_partial() -> None:
        """TC-S1-14：部分定位返回 found + unlocated，不整批失败。"""
        loc = ShardLocation(found={"a": "working", "b": "profile"}, unlocated=frozenset({"c"}))
        assert loc.shard_of("a") == "working"
        assert loc.shard_of("c") is None
        assert loc.shard_of("missing") is None
        assert loc.known_ids() == frozenset({"a", "b"})
