# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""逻辑分片的路由规则与定位契约（F09 D2 / D6）。

本模块只定义**storage 内部**的两个契约，不含任何具体分片名或后端知识：

- :class:`ShardResolver`：新记忆内容 → 分片名。只有 KV 包装层在 ``insert`` 时求值。
- :class:`ShardLocator`：批量 ``(scope, unit_id)`` → 生效归属分片（部分结果）。
  向量 / 全文 / 实体 / 图包装层经它跟随 KV，不自行按规则判定。

规则求值复用 ``list`` / ``search`` 的过滤语义（``matches_memory_unit`` /
``memory_field_value``），因此规则与 ``filters`` 对同一字段不会给出不同答案。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import FilterExpr, MemoryUnit, Scope
from jiuwen_memory.common.type_def.filter import iter_clauses, normalize
from jiuwen_memory.common.type_def.memory_filter import (
    matches_memory_unit,
    memory_field_value,
)

logger = get_logger(__name__)

#: 分片名契约（数据里的值从不直接拼进 SQL / 索引标识符，见 F09 D13）。
SHARD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: 归属记录保留前缀：部署把归属放进共享 KV 时，对外必须拒绝并隐藏该前缀。
SHARD_PLACEMENT_KEY_PREFIX = "/__shard_placement/"

#: 允许作为路由字段的裸字段名（F09 D4）。``system_metadata.*`` / ``user_metadata.*``
#: 走扁平路径，单独判定。
ALLOWED_ROUTING_FIELDS = frozenset({"tier", "tags", "source"})

#: 禁用路由字段：创建时多为默认值/系统盖章值，之后被频繁改写（F09 D4）。
DENIED_ROUTING_FIELDS = frozenset(
    {
        "lifecycle",
        "t_valid",
        "t_invalid",
        "t_ingest",
        "t_event",
        "t_message",
        # 结构投影字段：随树结构调整被内核改写。
        "hierarchy_kind",
        "hierarchy_role",
        "hierarchy_status",
        "tree_parent_id",
        "tree_span_start",
        "tree_span_end",
        # 标识 / 内部投影字段。
        "unit_id",
        "id",
        "memory_type",
        "content_layer",
        "seq",
    }
)

#: system_metadata 中由内核改写或不参与来源继承的键，禁止作为路由字段。
DENIED_SYSTEM_METADATA_KEYS = frozenset({"middle", "infer", "procedural"})

ARRAY_ROUTING_FIELDS = frozenset({"tags"})

_METADATA_PREFIXES = ("system_metadata.", "user_metadata.")


def validate_shard_name(name: str) -> str:
    """校验分片名；非法时抛 :class:`~common.errors.ValidationError`。"""
    if not isinstance(name, str) or not SHARD_NAME_RE.match(name):
        raise ValidationError(
            f"invalid shard name {name!r}: must match {SHARD_NAME_RE.pattern}"
        )
    return name


def _validate_routing_field(field_name: str, *, for_shard_from: bool) -> None:
    """校验规则引用的字段属于受支持路由字段集合（启动期）。"""
    if not isinstance(field_name, str) or not field_name:
        raise ValidationError(f"routing field_name must be a non-empty string, got {field_name!r}")
    if field_name in DENIED_ROUTING_FIELDS:
        raise ValidationError(
            f"routing field_name {field_name!r} is not allowed: it is a system-managed field_name "
            "whose value changes after creation"
        )
    if field_name.startswith(_METADATA_PREFIXES):
        key = field_name.split(".", 1)[1]
        if not key:
            raise ValidationError(f"routing field_name {field_name!r} has an empty metadata key")
        if field_name.startswith("system_metadata.") and key in DENIED_SYSTEM_METADATA_KEYS:
            raise ValidationError(
                f"routing field_name {field_name!r} is a kernel-managed system metadata key"
            )
        return
    if field_name not in ALLOWED_ROUTING_FIELDS:
        raise ValidationError(
            f"routing field_name {field_name!r} is not supported; allowed: "
            f"{sorted(ALLOWED_ROUTING_FIELDS)} or system_metadata.* / user_metadata.*"
        )
    if for_shard_from and field_name in ARRAY_ROUTING_FIELDS:
        raise ValidationError(
            f"routing field {field_name!r} is an array field and cannot be used as shard_from"
        )


@dataclass(frozen=True)
class ShardRule:
    """一条有序规则：谓词分类（``match``）或直接指定（``shard_from``）。"""

    shard: str | None = None
    match: FilterExpr | None = None
    shard_from: str | None = None


class ShardResolver(ABC):
    """分片规则契约：新记忆内容 → 分片名（纯函数，启动配置冻结）。"""

    @property
    @abstractmethod
    def shards(self) -> tuple[str, ...]:
        """全部分片名（分片集合的唯一权威来源）。"""

    @property
    @abstractmethod
    def default_shard(self) -> str:
        """规则都不命中时的落点。"""

    @abstractmethod
    def resolve(self, unit: MemoryUnit) -> str:
        """返回该记忆创建时应落入的分片名。"""

    def resolve_on_update(
        self, old: MemoryUnit, new: MemoryUnit, current: str
    ) -> str | None:
        """``update`` 写入后需要搬去的分片；``None`` 表示保持当前分片（F09 D16）。

        默认实现不搬迁；``rule_based`` 按「显式分片键优先、被删除不搬」的规则判定。
        """
        return None


class RuleBasedShardResolver(ShardResolver):
    """默认实现：``rule_based``（有序规则 + ``default_shard``）。"""

    def __init__(
        self,
        *,
        shards: list[str] | tuple[str, ...],
        rules: list[dict],
        default_shard: str | None = None,
        on_unknown: str = "reject",
    ) -> None:
        if not shards:
            raise ValidationError("shard_resolver.shards must not be empty")
        cleaned: list[str] = []
        for name in shards:
            validate_shard_name(name)
            if name not in cleaned:
                cleaned.append(name)
        self._shards = tuple(cleaned)
        if on_unknown not in ("reject", "default"):
            raise ValidationError(
                f"shard_resolver.on_unknown must be 'reject' or 'default', got {on_unknown!r}"
            )
        self._on_unknown = on_unknown
        target = default_shard or self._shards[0]
        if target not in self._shards:
            raise ValidationError(
                f"shard_resolver.default_shard {target!r} is not in shards {list(self._shards)}"
            )
        self._default = target
        self._rules = tuple(self._parse_rules(rules))

    def _parse_rules(self, rules: list[dict]) -> list[ShardRule]:
        parsed: list[ShardRule] = []
        for raw in rules:
            if not isinstance(raw, dict):
                raise ValidationError(f"shard rule must be an object, got {type(raw).__name__}")
            unknown = set(raw) - {"shard", "match", "shard_from"}
            if unknown:
                raise ValidationError(f"shard rule has unknown keys: {sorted(unknown)}")
            has_match = "match" in raw
            has_from = "shard_from" in raw
            if has_match == has_from:
                raise ValidationError(
                    "shard rule requires exactly one of 'match' or 'shard_from'"
                )
            if has_from:
                field_name = raw["shard_from"]
                if not isinstance(field_name, str) or not field_name:
                    raise ValidationError("shard_from must be a non-empty field path")
                _validate_routing_field(field_name, for_shard_from=True)
                parsed.append(ShardRule(shard_from=field_name))
                continue
            shard = raw.get("shard")
            if not isinstance(shard, str) or shard not in self._shards:
                raise ValidationError(
                    f"shard rule targets unconfigured shard {shard!r}; "
                    f"configured shards: {list(self._shards)}"
                )
            self._validate_raw_match(raw["match"])
            expr = normalize(raw["match"])
            if expr is None:
                raise ValidationError("shard rule match must not be empty")
            for clause in iter_clauses(expr):
                _validate_routing_field(clause.field, for_shard_from=False)
            parsed.append(ShardRule(shard=shard, match=expr))
        return parsed

    @staticmethod
    def _validate_raw_match(raw: object) -> None:
        """在 dict DSL 规范化**之前**校验原始字段名。

        ``normalize`` 会把不认识的裸字段改写成 ``user_metadata.<name>``，若只校验
        规范化后的叶子，``hierarchy_id`` / ``tree_id`` 这类禁用字段会被伪装成
        metadata 键绕过 D4。这里按调用方原始写法判定。
        """
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in {"AND", "OR", "NOT"}:
                    children = value if isinstance(value, list) else [value]
                    for child in children:
                        RuleBasedShardResolver._validate_raw_match(child)
                    continue
                if isinstance(key, str) and key and "." not in key:
                    if key in DENIED_ROUTING_FIELDS or key.startswith(("hierarchy_", "tree_")):
                        raise ValidationError(
                            f"routing field {key!r} is not allowed: it is a "
                            "system-managed field whose value changes after creation"
                        )

    @property
    def shards(self) -> tuple[str, ...]:
        return self._shards

    @property
    def default_shard(self) -> str:
        return self._default

    def resolve(self, unit: MemoryUnit) -> str:
        for rule in self._rules:
            if rule.shard_from is not None:
                value = memory_field_value(unit, rule.shard_from)
                if value is None or value == "":
                    continue  # 显式未指定：让位给后续规则
                if not isinstance(value, str) or value not in self._shards:
                    return self._on_unknown_value(rule.shard_from, value)
                return value
            if matches_memory_unit(unit, rule.match):
                return rule.shard or self._default
        return self._default

    def resolve_on_update(
        self, old: MemoryUnit, new: MemoryUnit, current: str
    ) -> str | None:
        """显式分片键优先；字段被删除 / 置空时保持原分片，其余按规则对新内容求值。"""
        for rule in self._rules:
            if rule.shard_from is None:
                continue
            new_value = memory_field_value(new, rule.shard_from)
            old_value = memory_field_value(old, rule.shard_from)
            if new_value is None or new_value == "":
                if old_value is not None and old_value != "":
                    # 显式键被删除 / 置空：删除不等于想换分片（R19）。
                    return None
                continue
            if not isinstance(new_value, str) or new_value not in self._shards:
                target = self._on_unknown_value(rule.shard_from, new_value)
            else:
                target = new_value
            return None if target == current else target
        target = self.resolve(new)
        return None if target == current else target

    def _on_unknown_value(self, field_name: str, value: object) -> str:
        message = (
            f"shard_from {field_name!r} produced unknown shard {value!r}; "
            f"configured shards: {list(self._shards)}"
        )
        if self._on_unknown == "reject":
            raise ValidationError(message)
        logger.warning("%s; falling back to default_shard %r", message, self._default)
        return self._default


@dataclass(frozen=True)
class ShardLocation:
    """批量定位结果：含 ``active`` 与 ``moving`` 归属的 id（部分结果）。

    ``moving`` 中的 id 同时出现在 ``found``（值为源分片）里：读取仍以源分片为准，
    派生写入据此拒绝该条（F09 D16）。
    """

    found: dict[str, str] = field(default_factory=dict)
    unlocated: frozenset[str] = frozenset()
    moving: frozenset[str] = frozenset()

    def shard_of(self, unit_id: str) -> str | None:
        """返回该 id 的生效分片；未定位返回 ``None``。"""
        return self.found.get(unit_id)

    def known_ids(self) -> frozenset[str]:
        """已定位的 id 集合。"""
        return frozenset(self.found)


class ShardLocator(ABC):
    """定位契约：批量 ``(scope, unit_id)`` → 分片（只读归属记录，不缓存）。"""

    @abstractmethod
    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        """返回部分结果；单个 id 缺失不使整批失败。"""


class ShardResolverProducer(Factory):
    """``shard_resolver`` 命名空间的注册式工厂（默认实现 ``rule_based``）。"""

    TOP_NAME = "shard_resolver"


@ShardResolverProducer.register("rule_based")
def _build_rule_based(config):
    return RuleBasedShardResolver(
        shards=config.get("shards") or [],
        rules=config.get("rules") or [],
        default_shard=config.get("default_shard"),
        on_unknown=config.get("on_unknown", "reject"),
    )
