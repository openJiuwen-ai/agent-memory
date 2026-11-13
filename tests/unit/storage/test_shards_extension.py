# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 S5：``extensions["shards"]`` 透传、真源校验、``get_in_shards`` 与展开器。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import (
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.type_def import (
    HierarchyRole,
    MemoryTier,
    MemoryUnit,
    ParsedQuery,
    RecallChannel,
    RetrievalPipeline,
    Scope,
    ScoredUnit,
    Segment,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.retrieval.expander import ExpandRequest
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.domain_store_impl.composite_domain_store import (
    CompositeDomainStore,
)
from jiuwen_memory.storage.domain_store_impl.recaller import Recaller
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import ShardedGetResult
from tests.unit.retrieval.expansion_fixtures import (
    Collector,
    expand_request,
    link,
    tree_unit,
)
from tests.unit.retrieval.hierarchy_query_fixtures import QUERY_SCOPE

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile", "main")


def unit(uid: str, *, tier: MemoryTier = MemoryTier.CORE) -> MemoryUnit:
    return MemoryUnit(
        id=uid, scope=SCOPE, tier=tier, segments=[Segment(content=f"content-{uid}")]
    )


def build_stack() -> tuple[CompositeStoreManager, CompositeDomainStore, ShardRoutedKVStore]:
    resolver = RuleBasedShardResolver(
        shards=list(SHARDS),
        default_shard="main",
        rules=[
            {"shard": "working", "match": {"tier": "working"}},
            {"shard": "profile", "match": {"tier": "core"}},
        ],
    )
    placement = InMemoryKVStore()
    kv = ShardRoutedKVStore(
        resolver=resolver,
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores={name: InMemoryKVStore() for name in SHARDS},
    )
    manager = CompositeStoreManager(kv=kv)
    domain = CompositeDomainStore(
        manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK
    )
    manager.bind_domain_store(domain)
    domain.add(SCOPE, [unit("u1"), unit("u2", tier=MemoryTier.WORKING)])
    return manager, domain, kv


class FakeRecaller(Recaller):
    def __init__(self, ids: list[str], *, supports: bool = True) -> None:
        self._ids = ids
        self._supports = supports

    @staticmethod
    def channel() -> RecallChannel:
        return RecallChannel.KEYWORD

    def supports_shards(self) -> bool:
        return self._supports

    def recall(self, scope, query, top_k):
        return [ScoredUnit(unit_id=uid, channel=RecallChannel.KEYWORD) for uid in self._ids]

    @staticmethod
    def health() -> None:
        return None


def test_get_in_shards_three_way_result() -> None:
    """TC-S5-5：found / missing_ids / excluded_ids。"""
    _, domain, _ = build_stack()
    result = domain.get_in_shards(
        SCOPE, ["u1", "u2", "missing"], frozenset({"profile"})
    )
    assert [item.id for item in result.found] == ["u1"]
    assert result.excluded_ids == ["u2"]
    assert result.missing_ids == ["missing"]


def test_get_in_shards_requires_shards() -> None:
    _, domain, _ = build_stack()
    with pytest.raises(ValidationError):
        domain.get_in_shards(SCOPE, ["u1"], frozenset())


def test_get_in_shards_rejects_unknown_shard_names() -> None:
    """拼写错误的分片名必须报错，不能伪装成「记忆不在目标分片」。"""
    kv, domain, _ = build_stack()
    with pytest.raises(ValidationError, match="unknown shard"):
        domain.get_in_shards(SCOPE, ["u1"], frozenset({"typo"}))
    with pytest.raises(ValidationError, match="unknown shard"):
        domain.get_in_shards(SCOPE, ["u1"], frozenset({"profile", "typo"}))


def test_recall_filters_candidates_by_effective_shard() -> None:
    """TC-S5-1 / TC-S5-7：召回结果按生效归属收窄。"""
    _, domain, _ = build_stack()
    domain.bind_recallers([FakeRecaller(["u1", "u2"])])
    result = domain.recall(
        SCOPE,
        ParsedQuery(raw="x", extensions={"shards": ["profile"]}),
        channels=None,
        recall_limit=10,
    )
    assert [candidate.unit_id for candidate in result.batches[0].candidates] == ["u1"]


def test_recall_rejects_unsupported_channel() -> None:
    """TC-S5-4：启用通道不支持分片时报错，不静默返回全部。"""
    _, domain, _ = build_stack()
    domain.bind_recallers([FakeRecaller(["u1"], supports=False)])
    # S09 错误语义：请求使用未声明能力走统一的「不支持」，不是参数非法。
    with pytest.raises(UnsupportedStorageCapabilityError):
        domain.recall(
            SCOPE,
            ParsedQuery(raw="x", extensions={"shards": ["profile"]}),
            channels=None,
            recall_limit=10,
        )


@pytest.mark.parametrize("value", [[], None, "profile", ["ghost"], ["profile", ""]])
def test_shards_extension_validation(value) -> None:
    _, domain, _ = build_stack()
    domain.bind_recallers([FakeRecaller(["u1"])])
    with pytest.raises(ValidationError):
        domain.recall(
            SCOPE,
            ParsedQuery(raw="x", extensions={"shards": value}),
            channels=None,
            recall_limit=10,
        )


def test_invalid_shards_rejected_even_when_no_channel_selected() -> None:
    """非法 shards 不能因为恰好没有选中召回通道而被放行（检索时序图步骤 2）。"""
    _, domain, _ = build_stack()
    domain.bind_recallers([])
    with pytest.raises(ValidationError):
        domain.recall(
            SCOPE,
            ParsedQuery(raw="x", extensions={"shards": ["ghost"]}),
            channels=None,
            recall_limit=10,
        )


def test_no_shards_key_keeps_full_scan() -> None:
    _, domain, _ = build_stack()
    domain.bind_recallers([FakeRecaller(["u1", "u2"])])
    result = domain.recall(
        SCOPE, ParsedQuery(raw="x"), channels=None, recall_limit=10
    )
    assert {candidate.unit_id for candidate in result.batches[0].candidates} == {"u1", "u2"}


def test_get_ignores_shards() -> None:
    """TC-S5-3：按 id 的 get 忽略 shards。"""
    _, domain, _ = build_stack()
    units = domain.get(SCOPE, ["u1", "u2"])
    assert {item.id for item in units} == {"u1", "u2"}


def test_domain_store_without_shard_support_rejects() -> None:
    """TC-S5-4：未启用分片的 DomainStore 在带 shards 时明确报不支持。"""

    class BareDomain(DomainStore):
        @property
        def security(self):
            raise NotImplementedError

        @staticmethod
        def preferred_retrieval_pipeline():
            return RetrievalPipeline.RECALL_GET_RANK

        @staticmethod
        def scopes(**kwargs):
            return []

        @staticmethod
        def add(scope, units, *, mode=None, access=None, **kwargs):
            return None

        @staticmethod
        def update(scope, units, *, mode=None, access=None, **kwargs):
            return None

        @staticmethod
        def delete(scope, unit_ids, *, mode=None, access=None, **kwargs):
            return None

        @staticmethod
        def get(scope, unit_ids, *, access=None, **kwargs):
            return []

        @staticmethod
        def list(scope, *, offset=0, limit=100, memory_types=None, filters=None,
                 extensions=None, access=None, **kwargs):
            raise NotImplementedError

        @staticmethod
        def recall(scope, query, *, channels, recall_limit, access=None, **kwargs):
            raise NotImplementedError

        @staticmethod
        def recall_and_get(scope, query, *, channels, recall_limit, access=None,
                          **kwargs):
            raise NotImplementedError

        @staticmethod
        def retrieve(scope, query, fuser, *, channels, recall_limit, rank_limit,
                     access=None, **kwargs):
            raise NotImplementedError

        @staticmethod
        def read_md_file(scope, md_basename, *, project="", line_range=None,
                         access=None):
            raise NotImplementedError

        @staticmethod
        def edit_md_file(scope, md_basename, old_text, new_text, *, project="",
                         access=None):
            raise NotImplementedError

        @staticmethod
        def health() -> None:
            return None

    with pytest.raises(UnsupportedStorageCapabilityError):
        BareDomain().get_in_shards(SCOPE, ["u1"], frozenset({"profile"}))


class _ExcludingDomain:
    """只有子节点在其它分片：get_in_shards 返回 excluded。"""

    def requested_shards(self, extensions):
        return DomainStore.requested_shards(self, extensions)

    @staticmethod
    def get_in_shards(scope, unit_ids, shards):
        return ShardedGetResult(found=[], missing_ids=[], excluded_ids=list(unit_ids))

    @staticmethod
    def get(scope, unit_ids):
        raise AssertionError("expander must use get_in_shards when shards is requested")


def test_expander_records_shard_excluded() -> None:
    """TC-S5-6：展开器带 shards 时排除其它分片子记忆并记 shard_excluded。"""
    root = tree_unit("root")
    children = [
        tree_unit("first", HierarchyRole.SNAPSHOT),
        tree_unit("second", HierarchyRole.SNAPSHOT),
    ]
    link(root, children)
    collector = Collector()
    request = expand_request(root, collector)
    request = ExpandRequest(
        root=request.root,
        query=replace(request.query, extensions={"shards": ["working"]}),
        depth=1,
        select=request.select,
        seen=request.seen,
        node_limit=request.node_limit,
    )
    result = DefaultExpander(_ExcludingDomain()).expand(QUERY_SCOPE, request)
    codes = {issue.code for issue in result.issues}
    assert "shard_excluded" in codes
    assert collector.selected == []


def test_keyword_recaller_requires_entity_shard_support() -> None:
    """P2：实体通道不支持分片时，关键词通道不得声称支持。"""
    from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
        WhitespaceTokenizer,
    )
    from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
    from jiuwen_memory.storage.entity_impl.in_memory_entity_store import (
        InMemoryEntityStore,
    )
    from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import (
        InMemoryFulltextStore,
    )

    manager = CompositeStoreManager(
        kv=InMemoryKVStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
        entity=InMemoryEntityStore(),
    )
    recaller = KeywordRecaller(manager, entity_store=manager.entity())
    assert recaller.supports_shards() is False


def test_shard_routed_scan_does_not_leak_placement() -> None:
    """保留前缀与非本分片数据不出现在逻辑 scan（S2 的跨层回归）。"""
    _, _, kv = build_stack()
    keys = [key for key, _ in kv.scan(SCOPE)]
    assert memory_key("u1") in keys
    assert all(not key.startswith("/__shard_placement/") for key in keys)


def test_domain_store_owns_shard_key_parsing() -> None:
    """``extensions["shards"]`` 的语义归数据面：``[]``/``None`` 不得当成「未指定」。"""
    _, domain, _ = build_stack()
    assert domain.requested_shards({}) is None
    assert domain.requested_shards({"shards": ["working"]}) == frozenset({"working"})
    for invalid in ([], None, "working", 5, ["working", ""]):
        with pytest.raises(ValidationError):
            domain.requested_shards({"shards": invalid})
    with pytest.raises(ValidationError):
        domain.requested_shards({"shards": ["ghost"]})
