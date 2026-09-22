"""CandidateResolver——四源统一 resolve()（F04 D3）。

验证四实现全部由已有存储原语组合（list_units / load_units / Retriever /
scopes()）：筛选下推（含 t_ingest 时间窗）、差集回显、保序去重、父 scope
覆盖约束。
"""

# Pytest 类只用于分组，测试方法按 pytest 约定保留实例形态。
# pylint: disable=add-staticmethod-or-classmethod-decorator

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    CandidateOutcome,
    FilterClause,
    FilterOp,
    MemoryUnit,
    PredicateCandidate,
    RecallChannel,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.jobs_impl.candidate_resolver import (
    MAX_CANDIDATE_UNITS,
    MAX_FAN_OUT_BUCKETS,
    FanOutResolver,
    IdsResolver,
    PredicateResolver,
    RecallResolver,
    _effective_filters,
    covered_by,
)
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, RetrievedItem
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.conftest import make_storage

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_unit(
    uid: str,
    scope: Scope,
    content: str,
    *,
    t_ingest: datetime | None = None,
    tags: list[str] | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        segments=[Segment(content=content)],
        temporal=Temporal(t_ingest=t_ingest),
        tags=tags or [],
    )


def _insert(kv: InMemoryKVStore, unit: MemoryUnit) -> None:
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))


# ---------------------------------------------------------------------------
# 覆盖约束 + 时间窗合并（纯函数）
# ---------------------------------------------------------------------------


class TestCoveredBy:
    def test_org_parent_covers_all_children(self) -> None:
        parent = Scope(org="acme")
        assert covered_by(parent, Scope(org="acme", user="alice"))
        assert covered_by(parent, Scope(org="acme", space="s1", agent="a1"))
        assert not covered_by(parent, Scope(org="other", user="alice"))

    def test_full_tuple_covers_only_itself(self) -> None:
        parent = Scope(org="acme", space="s", user="u", agent="a", session="x")
        sibling = Scope(org="acme", space="s", user="u", agent="a", session="y")
        assert covered_by(parent, parent)
        assert not covered_by(parent, sibling)


class TestCoveredByShape:
    """形状约束：前缀命中后的单向收窄（只剔除、不放大）。"""

    _USERS_ONLY = frozenset({"agent", "session"})

    def test_require_empty_excludes_agent_and_session_buckets(self) -> None:
        parent = Scope(org="acme")
        kw = {"require_empty": self._USERS_ONLY}
        assert covered_by(parent, Scope(org="acme"), **kw), "org 共享桶"
        assert covered_by(parent, Scope(org="acme", user="u1"), **kw), "用户桶"
        assert not covered_by(parent, Scope(org="acme", user="u1", agent="a1"), **kw)
        assert not covered_by(parent, Scope(org="acme", user="u1", session="s1"), **kw)

    def test_require_nonempty_excludes_org_shared_bucket(self) -> None:
        """「公司所有用户桶」= 前缀 org + user 必须非空。"""
        parent = Scope(org="acme")
        kw = {"require_nonempty": frozenset({"user"})}
        assert not covered_by(parent, Scope(org="acme"), **kw), "共享桶被排除"
        assert covered_by(parent, Scope(org="acme", user="u1"), **kw)

    def test_org_shared_only(self) -> None:
        """「公司所有共享/群体桶」= 前缀 org + user 必须为空。"""
        parent = Scope(org="acme")
        kw = {"require_empty": frozenset({"user"})}
        assert covered_by(parent, Scope(org="acme"), **kw)
        assert covered_by(parent, Scope(org="acme", space="team1"), **kw), "群体桶"
        assert not covered_by(parent, Scope(org="acme", user="u1"), **kw)

    def test_default_kwargs_equal_legacy_prefix_only(self) -> None:
        """缺省空集 = 纯前缀判定（向后兼容锚点）。"""
        parent = Scope(org="acme")
        for child in (
            Scope(org="acme"),
            Scope(org="acme", user="u1"),
            Scope(org="acme", user="u1", agent="a1", session="s9"),
        ):
            assert covered_by(parent, child, frozenset(), frozenset()) == covered_by(
                parent, child
            )


class TestEffectiveFilters:
    def test_no_window_returns_static(self) -> None:
        static = FilterClause("tier", FilterOp.EQ, "episodic")
        assert _effective_filters(static, None) is static
        assert _effective_filters(None, None) is None
        assert _effective_filters(None, 0) is None

    def test_window_merges_t_ingest_gte(self) -> None:
        merged = _effective_filters(FilterClause("tier", FilterOp.EQ, "episodic"), 3600)
        assert merged is not None
        # and_merge 包装：静态子句 + t_ingest GTE（毫秒）同为本体 AND 子节点
        clauses = list(getattr(merged, "children", [merged]))
        fields = {c.field for c in clauses}
        assert fields == {"tier", "t_ingest"}
        t_ingest_clause = next(c for c in clauses if c.field == "t_ingest")
        expected = int((_now() - timedelta(seconds=3600)).timestamp() * 1000)
        assert abs(t_ingest_clause.value - expected) < 5_000, "cutoff 应为 now-window 毫秒"


# ---------------------------------------------------------------------------
# ① 谓词源
# ---------------------------------------------------------------------------


class TestPredicateResolver:
    def test_lists_all_scope_units(self) -> None:
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("u1", scope, "one"))
        _insert(kv, _make_unit("u2", scope, "two"))
        _insert(kv, _make_unit("x", Scope(org="acme", user="u2"), "other"))

        outcome = asyncio.run(PredicateResolver(scope, kv).resolve())

        assert len(outcome.groups) == 1
        assert outcome.groups[0].scope == scope
        assert {u.id for u in outcome.groups[0].units} == {"u1", "u2"}

    def test_window_pushes_t_ingest_cutoff(self) -> None:
        """window=秒 → t_ingest GTE cutoff 下推：旧记忆被存储层排除。"""
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        fresh, stale = _make_unit("fresh", scope, "new", t_ingest=_now()), _make_unit(
            "stale", scope, "old", t_ingest=_now() - timedelta(hours=2)
        )
        _insert(kv, fresh)
        _insert(kv, stale)

        outcome = asyncio.run(
            PredicateResolver(scope, kv, window=3600).resolve()
        )

        assert [u.id for u in outcome.groups[0].units] == ["fresh"]

    def test_static_filters_pushdown(self) -> None:
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("a", scope, "x", tags=["pinned"]))
        _insert(kv, _make_unit("b", scope, "y"))

        outcome = asyncio.run(
            PredicateResolver(
                scope, kv, filters=FilterClause("tags", FilterOp.CONTAINS, "pinned")
            ).resolve()
        )

        assert [u.id for u in outcome.groups[0].units] == ["a"]

    def test_rejects_unbounded_candidate_set(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwen_memory.control.jobs_impl.candidate_resolver.list_units",
            lambda *_args, **_kwargs: ([], MAX_CANDIDATE_UNITS + 1),
        )
        with pytest.raises(ValidationError, match="单次上限"):
            asyncio.run(PredicateResolver(Scope(org="acme"), InMemoryKVStore()).resolve())


# ---------------------------------------------------------------------------
# ② 点名源
# ---------------------------------------------------------------------------


class TestIdsResolver:
    def test_point_read_with_diff_feedback(self) -> None:
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("u1", scope, "one"))
        _insert(kv, _make_unit("u2", scope, "two"))

        outcome = asyncio.run(
            IdsResolver(scope, kv, ["u2", "missing", "u1"]).resolve()
        )

        assert [u.id for u in outcome.groups[0].units] == ["u2", "u1"], "保序"
        assert outcome.requested_ids == ["u2", "missing", "u1"]
        assert outcome.loaded_ids == ["u2", "u1"]
        assert outcome.skipped == ["missing:not_found"]

    def test_dedupes_repeated_ids(self) -> None:
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("u1", scope, "one"))

        outcome = asyncio.run(IdsResolver(scope, kv, ["u1", "u1"]).resolve())

        assert outcome.requested_ids == ["u1"], "去重保序，差集不被稀释"


# ---------------------------------------------------------------------------
# ③ 召回源
# ---------------------------------------------------------------------------


class _StubRetriever(Retriever):
    """记录查询并返回固定 unit_ids 的 Retriever 替身。"""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.queries: list[tuple[Scope, RetrievalQuery]] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        self.queries.append((scope, query))
        return self.result


class TestRecallResolver:
    def test_recall_then_point_read(self) -> None:
        scope = Scope(org="acme", user="u1")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("r1", scope, "one"))
        _insert(kv, _make_unit("r2", scope, "two"))
        # 融合后跨通道重复命中 r1 —— 应去重
        stub = _StubRetriever(
            RetrievalResult(
                items=[
                    RetrievedItem(unit_id="r1", score=0.9),
                    RetrievedItem(unit_id="r2", score=0.8),
                    RetrievedItem(unit_id="r1", score=0.7),
                ]
            )
        )

        outcome = asyncio.run(
            RecallResolver(
                scope, kv, stub, {"text": "q"}, ["vector"], top_k=10
            ).resolve()
        )

        assert [u.id for u in outcome.groups[0].units] == ["r1", "r2"]
        # query/channels/top_k 透传到检索层
        _, q = stub.queries[0]
        assert q.text == "q"
        assert q.top_k == 10
        assert q.channels == [RecallChannel("vector")]

    def test_filters_passthrough_to_retrieval_query(self) -> None:
        scope = Scope(org="acme", user="u1")
        stub = _StubRetriever(RetrievalResult())
        filters = FilterClause("tier", FilterOp.EQ, "episodic")

        resolver = RecallResolver(
            scope, InMemoryKVStore(), stub, {"text": "x"}, [], filters=filters
        )
        asyncio.run(resolver.resolve())

        assert stub.queries[0][1].filters == filters


# ---------------------------------------------------------------------------
# ④ 枚举源
# ---------------------------------------------------------------------------


def test_predicate_resolver_reads_domain_store_data_plane() -> None:
    scope = Scope(org="acme", user="domain")
    kv = InMemoryKVStore()
    unit = _make_unit("domain-1", scope, "stored through domain")
    domain_store = make_storage(kv=kv).domain_store()
    domain_store.add(scope, [unit])

    outcome = asyncio.run(PredicateResolver(scope, domain_store).resolve())

    assert [item.id for item in outcome.groups[0].units] == ["domain-1"]


class TestFanOutResolver:
    def test_rejects_too_many_buckets(self) -> None:
        buckets = [Scope(org="acme", user=f"u{i}") for i in range(MAX_FAN_OUT_BUCKETS + 1)]
        with pytest.raises(ValidationError, match="桶数超过"):
            asyncio.run(
                FanOutResolver(
                    Scope(org="acme"),
                    InMemoryKVStore(),
                    PredicateCandidate(),
                    buckets=buckets,
                ).resolve()
            )

    def test_rejects_total_units_across_buckets(self, monkeypatch) -> None:
        parent = Scope(org="acme")
        alice, bob = Scope(org="acme", user="alice"), Scope(org="acme", user="bob")
        kv = InMemoryKVStore()
        for scope, uid in ((alice, "a1"), (alice, "a2"), (bob, "b1"), (bob, "b2")):
            _insert(kv, _make_unit(uid, scope, uid))
        monkeypatch.setattr(
            "jiuwen_memory.control.jobs_impl.candidate_resolver.MAX_CANDIDATE_UNITS",
            3,
        )

        with pytest.raises(ValidationError, match="候选总量超过"):
            asyncio.run(
                FanOutResolver(parent, kv, PredicateCandidate()).resolve()
            )

    def test_buckets_per_covered_scope(self) -> None:
        parent = Scope(org="acme")
        alice, bob = Scope(org="acme", user="alice"), Scope(org="acme", user="bob")
        outsider = Scope(org="other", user="carol")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("a1", alice, "alice one"))
        _insert(kv, _make_unit("a2", alice, "alice two"))
        _insert(kv, _make_unit("b1", bob, "bob one"))
        _insert(kv, _make_unit("c1", outsider, "carol"))

        outcome = asyncio.run(
            FanOutResolver(parent, kv, PredicateCandidate()).resolve()
        )

        assert isinstance(outcome, CandidateOutcome)
        buckets = {g.scope.user: g.units for g in outcome.groups}
        assert set(buckets) == {"alice", "bob"}, "org=other 不入桶"
        assert {u.id for u in buckets["alice"]} == {"a1", "a2"}
        assert {u.id for u in buckets["bob"]} == {"b1"}

    def test_child_window_applies_per_bucket(self) -> None:
        parent = Scope(org="acme")
        alice = Scope(org="acme", user="alice")
        kv = InMemoryKVStore()
        _insert(kv, _make_unit("fresh", alice, "n", t_ingest=_now()))
        _insert(kv, _make_unit("stale", alice, "o", t_ingest=_now() - timedelta(hours=3)))

        outcome = asyncio.run(
            FanOutResolver(parent, kv, PredicateCandidate(window=3600)).resolve()
        )

        assert [u.id for u in outcome.groups[0].units] == ["fresh"]

    def test_shape_require_empty_selects_user_buckets_only(self) -> None:
        """「批量给公司所有注册用户独立更新」：agent/session 桶不入批。"""
        parent = Scope(org="acme")
        shared, alice, bob = (
            Scope(org="acme"),
            Scope(org="acme", user="alice"),
            Scope(org="acme", user="bob"),
        )
        agent_bucket = Scope(org="acme", user="alice", agent="a1")
        session_bucket = Scope(org="acme", user="bob", agent="b1", session="s9")
        kv = InMemoryKVStore()
        for scope, uid in (
            (shared, "sh1"),
            (alice, "a1u"),
            (bob, "b1u"),
            (agent_bucket, "a1ag"),
            (session_bucket, "b1se"),
        ):
            _insert(kv, _make_unit(uid, scope, uid))

        outcome = asyncio.run(
            FanOutResolver(
                parent,
                kv,
                PredicateCandidate(),
                require_empty=frozenset({"agent", "session"}),
            ).resolve()
        )

        keys = {(g.scope.space, g.scope.user, g.scope.agent) for g in outcome.groups}
        assert keys == {("", "", ""), ("", "alice", ""), ("", "bob", "")}, (
            "agent 桶与会话桶被形状约束剔除"
        )
        assert {u.id for g in outcome.groups for u in g.units} == {"sh1", "a1u", "b1u"}

    def test_shape_cross_space_per_user(self) -> None:
        """u1 跨 space 的用户桶全收，agent/session 子桶剔除。"""
        parent = Scope(org="acme", user="u1")
        default_user = Scope(org="acme", space="", user="u1")
        team1_user = Scope(org="acme", space="team1", user="u1")
        team1_session = Scope(org="acme", space="team1", user="u1", agent="a1", session="s")
        kv = InMemoryKVStore()
        for scope, uid in (
            (default_user, "d1"),
            (team1_user, "t1"),
            (team1_session, "ts"),
        ):
            _insert(kv, _make_unit(uid, scope, uid))

        outcome = asyncio.run(
            FanOutResolver(
                parent,
                kv,
                PredicateCandidate(),
                require_empty=frozenset({"agent", "session"}),
            ).resolve()
        )

        keys = {(g.scope.space, g.scope.agent, g.scope.session) for g in outcome.groups}
        assert keys == {("", "", ""), ("team1", "", "")}, (
            "team1 的会话桶被形状约束剔除，default/team1 用户桶保留"
        )
        assert {u.id for g in outcome.groups for u in g.units} == {"d1", "t1"}
