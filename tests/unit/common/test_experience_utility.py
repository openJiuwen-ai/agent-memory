# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""经验记忆共用部件（common/experience.py）的单测，按源文件的节组织：

- 效用记录：键构造、事件幂等写入、复现作者、类别计数、统计记录与基线读写
- 取策略取值：首版取值的守恒（探索阈值、隔离期、半衰期等随策略版本调整）
- 质量分：加权计数、有效性、新鲜度与合成
- usage_ref：引用值的编解码（与接入方的回传约定）
- 聚合：效用事件 → 统计记录与类别基线（周期整理第 1 步，§2.5.2/§3.2）

构建侧的轨迹/判定记录等部件测试在 tests/unit/construction/test_experience.py。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.experience import (
    ATTR_HARMFUL,
    ATTR_HELPFUL,
    ATTR_NEUTRAL,
    BASELINE_KEY,
    CATEGORY_COUNTS_KEY_PREFIX,
    DEFAULT_BASELINE,
    ERASED_AUTHORS_KEY_PREFIX,
    EVIDENCE_EXTERNAL,
    EVIDENCE_MODEL_ONLY,
    EVIDENCE_TOOL_ERROR,
    EVIDENCE_USER_FEEDBACK,
    EVIDENCE_WEIGHTS,
    EXPLORATION_QUOTA,
    EXPLORATION_THRESHOLD,
    FRESHNESS_HALF_LIFE_DAYS,
    ISOLATION_PERIOD_DAYS,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    RANK_ALPHA,
    RANK_BETA,
    REPRO_AUTHOR_LIMIT,
    REPRO_AUTHORS_KEY_PREFIX,
    SMOOTHING_K,
    STATS_KEY_PREFIX,
    UTILITY_KEY_PREFIX,
    AttributionEvent,
    ReproductionEvent,
    StatsRecord,
    UtilityEventKey,
    UtilityStore,
    aggregate_utility,
    attribution_event_key,
    baseline_key,
    category_count_key,
    compute_baseline,
    encode_payload,
    encode_usage_ref,
    erased_author_key,
    freshness,
    parse_usage_ref,
    parse_utility_event_key,
    quality_score,
    repro_authors_key,
    reproduction_event_key,
    stats_key,
    validity,
    weighted_count,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

AUTHOR = "d" * 64  # 作者摘要（hex）
SCOPE = Scope(org="acme", user="u1")


# ====================================================================== #
# 效用记录：键构造与 UtilityStore
# ====================================================================== #
# 事件键含判定修订号、只 insert 的幂等语义（断点续做不重复计效用）与复现作者
# 至多 2 个的上限，都是周期整理与检索放行条件的直接输入——错了会让同一轨迹的
# 效用翻倍计入或放行条件提前满足。


class TestKeyBuilders:
    @staticmethod
    def test_attribution_event_key_contains_revision():
        key = attribution_event_key("e1", AUTHOR, "t1", 3)
        assert key == f"{UTILITY_KEY_PREFIX}e1/attr:{AUTHOR}:t1:3", (
            "归因事件键含条目 id、摘要、tid 与修订号"
        )

    @staticmethod
    def test_reproduction_event_key_contains_revision():
        key = reproduction_event_key("e1", AUTHOR, "t1", 0)
        assert key == f"{UTILITY_KEY_PREFIX}e1/repro:{AUTHOR}:t1:0", "复现事件键布局"

    @staticmethod
    def test_small_record_keys():
        assert stats_key("e1") == f"{STATS_KEY_PREFIX}e1", "统计键"
        assert repro_authors_key("e1") == f"{REPRO_AUTHORS_KEY_PREFIX}e1", "复现作者键"
        assert category_count_key("deploy") == f"{CATEGORY_COUNTS_KEY_PREFIX}deploy", (
            "类别计数键"
        )
        assert baseline_key() == BASELINE_KEY == "/experience/baseline", "类别基线键"


class TestUtilityStoreEvents:
    @staticmethod
    def test_attribution_event_written_once():
        store = UtilityStore(InMemoryKVStore())
        event = AttributionEvent(
            verdict="helpful", evidence="tool_error", author=AUTHOR, occurred_at=""
        )
        first = store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 0, event)
        again = store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 0, event)
        assert first is True, "首次写入返回 True"
        assert again is False, "同一事件（键含修订号）重复写入返回 False（幂等）"

    @staticmethod
    def test_different_revision_is_separate_event():
        store = UtilityStore(InMemoryKVStore())
        event = AttributionEvent(verdict="neutral")
        assert store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 0, event) is True
        assert store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 1, event) is True, (
            "不同修订号是不同事件，各自只记一次"
        )

    @staticmethod
    def test_reproduction_event_written_once():
        store = UtilityStore(InMemoryKVStore())
        event = ReproductionEvent(
            source_scope=["acme", "", "u2", "", ""],
            trajectory_id="t2",
            author=AUTHOR,
            retrieved_ids=["e1"],
            outcome="success",
        )
        assert store.write_reproduction_event(SCOPE, "e1", AUTHOR, "t2", 0, event) is True
        assert store.write_reproduction_event(SCOPE, "e1", AUTHOR, "t2", 0, event) is False, (
            "复现事件重复写入返回 False（幂等）"
        )


class TestReproAuthors:
    @staticmethod
    def test_load_missing_returns_empty():
        store = UtilityStore(InMemoryKVStore())
        assert store.load_repro_authors(SCOPE, "e1") == [], "无记录返回空列表"

    @staticmethod
    def test_add_appends_and_dedups():
        store = UtilityStore(InMemoryKVStore())
        first = store.add_repro_author(SCOPE, "e1", "d1")
        second = store.add_repro_author(SCOPE, "e1", "d1")
        assert first == ["d1"], "首次并入返回含该摘要的列表"
        assert second == ["d1"], "同一摘要不重复添加"

    @staticmethod
    def test_add_respects_author_limit():
        store = UtilityStore(InMemoryKVStore())
        store.add_repro_author(SCOPE, "e1", "d1")
        store.add_repro_author(SCOPE, "e1", "d2")
        authors = store.add_repro_author(SCOPE, "e1", "d3")
        assert len(authors) == REPRO_AUTHOR_LIMIT, "至多保留 2 个作者摘要"
        assert "d3" not in authors, "超出的摘要丢弃"

    @staticmethod
    def test_save_truncates_to_limit():
        store = UtilityStore(InMemoryKVStore())
        store.save_repro_authors(SCOPE, "e1", ["d1", "d2", "d3"])
        assert store.load_repro_authors(SCOPE, "e1") == ["d1", "d2"], "保存时截断到上限"


class TestErasedAuthors:
    """按作者清除的标记读写（mem2.0 §2.5.2「按作者清除」）。

    登记由删除回调写入（幂等覆盖）、周期整理聚合之前消费——删除不完整会让
    被清除作者的事件在下一轮聚合又计回统计。
    """

    @staticmethod
    def test_erased_author_key_layout():
        assert erased_author_key("d1") == f"{ERASED_AUTHORS_KEY_PREFIX}d1", (
            "清除标记键 = 前缀 + 作者摘要"
        )

    @staticmethod
    def test_add_and_load_erased_authors():
        store = UtilityStore(InMemoryKVStore())
        store.add_erased_author(SCOPE, "d1")
        store.add_erased_author(SCOPE, "d2")
        assert store.load_erased_authors(SCOPE) == ["d1", "d2"], "登记后可读回全部标记"

    @staticmethod
    def test_add_erased_author_idempotent():
        store = UtilityStore(InMemoryKVStore())
        store.add_erased_author(SCOPE, "d1")
        store.add_erased_author(SCOPE, "d1")
        assert store.load_erased_authors(SCOPE) == ["d1"], "重复登记幂等（同键覆盖）"

    @staticmethod
    def test_load_empty_scope_returns_empty():
        store = UtilityStore(InMemoryKVStore())
        assert store.load_erased_authors(SCOPE) == [], "无标记返回空列表"

    @staticmethod
    def test_drop_erased_author_removes_only_target():
        store = UtilityStore(InMemoryKVStore())
        store.add_erased_author(SCOPE, "d1")
        store.add_erased_author(SCOPE, "d2")
        store.drop_erased_author(SCOPE, "d1")
        assert store.load_erased_authors(SCOPE) == ["d2"], "只删目标标记"

    @staticmethod
    def test_drop_missing_erased_author_swallows():
        store = UtilityStore(InMemoryKVStore())
        store.drop_erased_author(SCOPE, "d1")  # 不存在也不抛
        assert store.load_erased_authors(SCOPE) == [], "缺失标记的删除静默通过"

    @staticmethod
    def test_delete_events_by_authors_removes_both_kinds():
        store = UtilityStore(InMemoryKVStore())
        attr = AttributionEvent(verdict="helpful", evidence="tool_error", author=AUTHOR)
        repro = ReproductionEvent(
            source_scope=["acme", "", "u2", "", ""],
            trajectory_id="t2",
            author=AUTHOR,
            retrieved_ids=["e1"],
            outcome="success",
        )
        other = AttributionEvent(verdict="neutral", author="e" * 64)
        store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 0, attr)
        store.write_reproduction_event(SCOPE, "e1", AUTHOR, "t2", 0, repro)
        store.write_attribution_event(SCOPE, "e1", "e" * 64, "t3", 0, other)

        removed = store.delete_events_by_authors(SCOPE, {AUTHOR})

        assert removed == 2, "删除被清除作者的归因与复现事件各一条"
        assert store.scan_events(SCOPE) and all(
            key.author != AUTHOR for key, _payload in store.scan_events(SCOPE)
        ), "被清除作者的事件全部消失"
        assert any(
            key.author == "e" * 64 for key, _payload in store.scan_events(SCOPE)
        ), "其他作者的事件保留"

    @staticmethod
    def test_delete_events_keeps_stats_and_repro_authors():
        """只删效用事件——统计记录与复现作者记录键前缀不同，不受影响。"""
        store = UtilityStore(InMemoryKVStore())
        attr = AttributionEvent(verdict="helpful", evidence="tool_error", author=AUTHOR)
        store.write_attribution_event(SCOPE, "e1", AUTHOR, "t1", 0, attr)
        store.save_stats(SCOPE, "e1", StatsRecord(covered=3))
        store.save_repro_authors(SCOPE, "e1", [AUTHOR])

        store.delete_events_by_authors(SCOPE, {AUTHOR})

        assert store.load_stats(SCOPE, "e1") is not None, "统计记录不被删除"
        assert store.load_repro_authors(SCOPE, "e1") == [AUTHOR], "复现作者记录不被删除"


class TestCategoryCounts:
    @staticmethod
    def test_load_missing_returns_zeroes():
        store = UtilityStore(InMemoryKVStore())
        assert store.load_category_count(SCOPE, "deploy") == {
            "active": 0,
            "dropped": 0,
        }, "无记录返回零计数"

    @staticmethod
    def test_add_accumulates_active_and_dropped():
        store = UtilityStore(InMemoryKVStore())
        store.add_category_count(SCOPE, "deploy", active=1)
        counts = store.add_category_count(SCOPE, "deploy", active=2, dropped=1)
        assert counts == {"active": 3, "dropped": 1}, "增量累加生效"
        assert store.load_category_count(SCOPE, "deploy") == {
            "active": 3,
            "dropped": 1,
        }, "累加结果落盘"

    @staticmethod
    def test_categories_independent():
        store = UtilityStore(InMemoryKVStore())
        store.add_category_count(SCOPE, "deploy", active=1)
        assert store.load_category_count(SCOPE, "config")["active"] == 0, (
            "类别计数按类别独立"
        )


class TestStatsAndBaseline:
    """统计记录与类别基线的读取（写入是周期整理的聚合步骤）。"""

    @staticmethod
    def test_load_stats_missing_returns_none():
        store = UtilityStore(InMemoryKVStore())
        assert store.load_stats(SCOPE, "e1") is None, "无统计记录返回 None（取基线）"

    @staticmethod
    def test_load_stats_roundtrip():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        payload = {
            "helpful": {"user_feedback": 2, "model_only": 5},
            "harmful": {"tool_error": 1},
            "covered": 8,
            "neutral": 3,
            "authors": ["d1", "d2"],
            "author_success": {"d1": 2},
            "strong_success": 1,
            "last_used_at": "2026-10-06T08:00:00Z",
            "last_positive_at": "2026-10-05T09:30:00Z",
            "merged_into": "",
            "competition_group": "g-12",
            "sealed": {"helpful": 0, "harmful": 0},
            "watermark": "2026-10-07T00:00:00Z",
        }
        kv.insert(SCOPE, stats_key("e1"), encode_payload(payload))
        record = store.load_stats(SCOPE, "e1")
        assert record is not None, "统计记录应读回"
        assert record == StatsRecord(
            helpful={"user_feedback": 2, "model_only": 5},
            harmful={"tool_error": 1},
            covered=8,
            neutral=3,
            authors=["d1", "d2"],
            author_success={"d1": 2},
            strong_success=1,
            last_used_at="2026-10-06T08:00:00Z",
            last_positive_at="2026-10-05T09:30:00Z",
            competition_group="g-12",
            sealed={"helpful": 0, "harmful": 0},
            watermark="2026-10-07T00:00:00Z",
        ), "全部字段按 §3.2 的结构读回"

    @staticmethod
    def test_load_stats_corrupted_returns_none():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        kv.insert(SCOPE, stats_key("e1"), b"\xff\xfe not json")
        assert store.load_stats(SCOPE, "e1") is None, "损坏记录按无统计记录处理"
        kv.insert(SCOPE, stats_key("e2"), encode_payload([1, 2]))
        assert store.load_stats(SCOPE, "e2") is None, "非对象记录按无统计记录处理"

    @staticmethod
    def test_load_baseline_missing_returns_empty():
        store = UtilityStore(InMemoryKVStore())
        assert store.load_baseline(SCOPE) == {}, "无基线返回空（逐类别回退默认）"

    @staticmethod
    def test_load_baseline_roundtrip():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        kv.insert(SCOPE, baseline_key(), encode_payload({"deploy": 0.6, "config": 0.4}))
        assert store.load_baseline(SCOPE) == {"deploy": 0.6, "config": 0.4}, (
            "基线按类别读回"
        )

    @staticmethod
    def test_load_baseline_corrupted_returns_empty():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        kv.insert(SCOPE, baseline_key(), b"\xff\xfe")
        assert store.load_baseline(SCOPE) == {}, "损坏基线按无记录处理"


# ====================================================================== #
# 取策略取值（首版守恒）
# ====================================================================== #


def test_strategy_defaults() -> None:
    """首版取值（§2.4 表）：改任何一项都应连着策略版本与 F08 一起改。"""
    assert EVIDENCE_WEIGHTS == {
        EVIDENCE_EXTERNAL: 1.0,
        EVIDENCE_USER_FEEDBACK: 1.0,
        EVIDENCE_TOOL_ERROR: 0.7,
        EVIDENCE_MODEL_ONLY: 0.4,
    }, "证据等级权重"
    assert math.isclose(SMOOTHING_K, 2.0), "平滑强度"
    assert (RANK_ALPHA, RANK_BETA) == (2.0, 1.0), "排序分指数：相关性平方"
    assert (EXPLORATION_THRESHOLD, EXPLORATION_QUOTA) == (5, 1), "探索阈值与名额"
    assert ISOLATION_PERIOD_DAYS == 7, "隔离期"
    assert math.isclose(FRESHNESS_HALF_LIFE_DAYS, 30.0), "新鲜度半衰期"
    assert math.isclose(DEFAULT_BASELINE, 0.5), "冷启动基线中性值"


# ====================================================================== #
# 质量分
# ====================================================================== #


class TestWeightedCount:
    @staticmethod
    def test_weights_each_evidence_level():
        counts = {
            EVIDENCE_EXTERNAL: 1,
            EVIDENCE_USER_FEEDBACK: 1,
            EVIDENCE_TOOL_ERROR: 1,
            EVIDENCE_MODEL_ONLY: 1,
        }
        assert weighted_count(counts) == pytest.approx(3.1), "1.0+1.0+0.7+0.4"

    @staticmethod
    def test_unknown_level_counts_zero():
        assert weighted_count({"mystery": 3}) == 0.0, "未知等级不计入"


class TestValidity:
    @staticmethod
    def test_zero_counts_fall_back_to_baseline():
        assert validity(StatsRecord(), 0.6) == pytest.approx(0.6), (
            "无统计记录的条目取基线（公式在零计数下退化为基线）"
        )

    @staticmethod
    def test_smoothed_toward_baseline():
        stats = StatsRecord(helpful={EVIDENCE_MODEL_ONLY: 1})
        # (0.4 + 2×0.5) / (0.4 + 0 + 2) = 1.4/2.4
        assert validity(stats, 0.5) == pytest.approx(1.4 / 2.4), (
            "k=2 的平滑：一次 model_only 有帮助不被全额计入"
        )

    @staticmethod
    def test_harmful_pulls_down():
        good = StatsRecord(helpful={EVIDENCE_EXTERNAL: 10})
        bad = StatsRecord(
            helpful={EVIDENCE_EXTERNAL: 10}, harmful={EVIDENCE_EXTERNAL: 10}
        )
        assert validity(bad, 0.5) < validity(good, 0.5), "加权有害拉低有效性"


class TestFreshness:
    @staticmethod
    def test_today_is_one():
        now = datetime.now(UTC)
        assert freshness(now.isoformat(), "", now=now) == pytest.approx(1.0), (
            "当天的时间点新鲜度为 1"
        )

    @staticmethod
    def test_half_life_decay():
        now = datetime.now(UTC)
        anchor = (now - timedelta(days=FRESHNESS_HALF_LIFE_DAYS)).isoformat()
        assert freshness(anchor, "", now=now) == pytest.approx(0.5), (
            "一个半衰期（30 天）前的时间点衰减到 0.5"
        )

    @staticmethod
    def test_prefers_last_positive_over_ingest():
        now = datetime.now(UTC)
        positive = (now - timedelta(days=60)).isoformat()
        ingest = (now - timedelta(days=1)).isoformat()
        value = freshness(positive, ingest, now=now)
        assert value == pytest.approx(freshness(positive, "", now=now)), (
            "优先按最近一次被复现/判有帮助的时间起算，不取落盘时间"
        )

    @staticmethod
    def test_no_usable_time_is_one():
        now = datetime.now(UTC)
        assert math.isclose(freshness("", "", now=now), 1.0), "无时间信息按最新鲜处理"
        assert math.isclose(freshness("not-a-date", "also-bad", now=now), 1.0), (
            "不可解析同上"
        )


def test_quality_score_is_weighted_sum() -> None:
    assert quality_score(0.8, 0.4) == pytest.approx(0.7 * 0.8 + 0.3 * 0.4), (
        "质量分 = 0.7×有效性 + 0.3×新鲜度"
    )


# ====================================================================== #
# usage_ref 引用值
# ====================================================================== #


class TestUsageRef:
    @staticmethod
    def test_encode_explore_and_normal():
        assert encode_usage_ref(explore=True) == "0.e", "探索名额标记 e"
        assert encode_usage_ref(explore=False) == "0.n", "按排序分标记 n"

    @staticmethod
    def test_parse_roundtrip():
        for ref in ("0.e", "0.n"):
            version, explore = parse_usage_ref(ref)
            assert encode_usage_ref(explore=explore, version=version) == ref, (
                "编解码往返一致"
            )

    @staticmethod
    def test_parse_invalid_defaults():
        assert parse_usage_ref("") == ("0", False), "空引用按版本 0 非探索"
        assert parse_usage_ref("garbage") == ("0", False), "不可解析同上"
        assert parse_usage_ref("0.x") == ("0", False), "非法标记同上"


# ====================================================================== #
# 聚合：效用事件 → 统计记录与类别基线（周期整理第 1 步，§2.5.2/§3.2）
# ====================================================================== #
# 聚合是周期整理的入口步骤：重放错了会让退休判定（归因次数）、有害退休（置信
# 下界）与复现放行（作者数）全部失真。时间字面量刻意固定，窗口边界（恰好 7 天）
# 是 ≥ 语义的直接断言点。

T0 = "2026-09-01T00:00:00+00:00"
T_PLUS_1D = "2026-09-02T00:00:00+00:00"
T_PLUS_3D = "2026-09-04T00:00:00+00:00"
T_PLUS_7D = "2026-09-08T00:00:00+00:00"
T_PLUS_10D = "2026-09-11T00:00:00+00:00"

AUTHOR_A = "a" * 64
AUTHOR_B = "b" * 64


def _attr_event(
    entry_id: str,
    author: str,
    trajectory_id: str,
    revision: int,
    *,
    verdict: str,
    evidence: str = EVIDENCE_MODEL_ONLY,
    occurred_at: str = T0,
) -> tuple[UtilityEventKey, dict[str, str]]:
    return (
        UtilityEventKey(entry_id, "attr", author, trajectory_id, revision),
        {"verdict": verdict, "evidence": evidence, "occurred_at": occurred_at},
    )


def _repro_event(
    entry_id: str,
    author: str,
    trajectory_id: str,
    revision: int,
    *,
    outcome: str,
    evidence: str = EVIDENCE_MODEL_ONLY,
    occurred_at: str = T0,
) -> tuple[UtilityEventKey, dict[str, str]]:
    return (
        UtilityEventKey(entry_id, "repro", author, trajectory_id, revision),
        {"outcome": outcome, "evidence": evidence, "occurred_at": occurred_at},
    )


class TestParseUtilityEventKey:
    @staticmethod
    def test_roundtrip_with_key_builders():
        for built, kind, revision in (
            (attribution_event_key("e1", AUTHOR_A, "t1", 3), "attr", 3),
            (reproduction_event_key("e1", AUTHOR_A, "t1", 0), "repro", 0),
        ):
            parsed = parse_utility_event_key(built)
            assert parsed == UtilityEventKey("e1", kind, AUTHOR_A, "t1", revision), (
                f"键构造的逆：{kind}"
            )

    @staticmethod
    def test_malformed_returns_none():
        for key in (
            "/experience/stats/e1",                         # 非事件前缀
            "/experience/utility/e1/attr:a:t1",             # 缺修订号
            "/experience/utility/e1/attr:a:t1:abc",         # 修订号非数字
            "/experience/utility/e1/judge:a:t1:0",          # 未知类型
            "/experience/utility/e1/attr:a:t1:0:extra",     # 多余段
        ):
            assert parse_utility_event_key(key) is None, f"坏布局返回 None：{key}"

    @staticmethod
    def test_entry_with_slash_not_parsed():
        assert parse_utility_event_key("/experience/utility/e/1/attr:a:t1:0") is None, (
            "条目 id 含 / 不在键布局内（写入方保证不含，防御性返回 None）"
        )


class TestAggregateLatestRevision:
    @staticmethod
    def test_same_group_takes_max_revision():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL),
            _attr_event("e1", AUTHOR_A, "t1", 2, verdict=ATTR_HARMFUL),
        ]
        stats = aggregate_utility(events)
        record = stats["e1"]
        assert record.covered == 1, "同 (条目, 类型, 作者, 轨迹) 只计最新修订"
        assert record.helpful == {}, "旧修订的结论被最新修订取代"
        assert record.harmful == {EVIDENCE_MODEL_ONLY: 1}, "最新修订的结论计入"

    @staticmethod
    def test_different_trajectory_or_author_both_counted():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HELPFUL),
            _attr_event("e1", AUTHOR_B, "t1", 0, verdict=ATTR_HELPFUL),
        ]
        stats = aggregate_utility(events, window_days=None)
        assert stats["e1"].covered == 3, (
            "不同轨迹、不同作者的组各自独立计入（窗口限计另管，另见窗口用例）"
        )


class TestAggregateAttribution:
    @staticmethod
    def test_counts_by_evidence_level():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL,
                        evidence=EVIDENCE_USER_FEEDBACK, occurred_at=T_PLUS_1D),
            _attr_event("e1", AUTHOR_B, "t2", 0, verdict=ATTR_HELPFUL,
                        evidence=EVIDENCE_USER_FEEDBACK, occurred_at=T_PLUS_3D),
            _attr_event("e1", AUTHOR_A, "t3", 0, verdict=ATTR_HARMFUL,
                        evidence=EVIDENCE_TOOL_ERROR, occurred_at=T_PLUS_7D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.helpful == {EVIDENCE_USER_FEEDBACK: 2}, "有帮助按证据等级分桶"
        assert record.harmful == {EVIDENCE_TOOL_ERROR: 1}, "有害同上"
        assert record.covered == 3, "覆盖次数只计有帮助与有害"
        assert record.last_positive_at == T_PLUS_3D, "最近一次判有帮助的时间"
        assert record.last_used_at == T_PLUS_7D, "最近一次取用取最大发生时间"

    @staticmethod
    def test_neutral_not_covered_nor_positive():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_NEUTRAL,
                        occurred_at=T_PLUS_3D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.neutral == 1, "判不计的次数单列"
        assert record.covered == 0, "neutral 不算覆盖"
        assert record.last_positive_at == "", "neutral 不更新最近正反馈"

    @staticmethod
    def test_watermark_written_to_all():
        events = [_attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL)]
        stats = aggregate_utility(events, watermark="2026-10-01T00:00:00+00:00")
        assert stats["e1"].watermark == "2026-10-01T00:00:00+00:00", "水位线随聚合写入"


class TestAggregateAttributionWindow:
    @staticmethod
    def test_same_author_within_window_counted_once():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL, occurred_at=T0),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HELPFUL, occurred_at=T_PLUS_3D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.covered == 1, "3 天 < 7 天窗口：同作者同结论只计一次"

    @staticmethod
    def test_exactly_window_edge_counts():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL, occurred_at=T0),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HELPFUL,
                        occurred_at=T_PLUS_7D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.covered == 2, "恰满 7 天算已出窗口（≥ 语义），再计一次"

    @staticmethod
    def test_beyond_window_counted_again():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL, occurred_at=T0),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HELPFUL,
                        occurred_at=T_PLUS_10D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.covered == 2, "10 天 > 7 天窗口：第二次照常计入"

    @staticmethod
    def test_helpful_and_harmful_windows_independent():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL, occurred_at=T0),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HARMFUL, occurred_at=T_PLUS_1D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.helpful == {EVIDENCE_MODEL_ONLY: 1}, "有帮助与有害的窗口各自独立"
        assert record.harmful == {EVIDENCE_MODEL_ONLY: 1}, "同上"

    @staticmethod
    def test_window_disabled_counts_all():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL, occurred_at=T0),
            _attr_event("e1", AUTHOR_A, "t2", 0, verdict=ATTR_HELPFUL, occurred_at=T_PLUS_1D),
            _attr_event("e1", AUTHOR_A, "t3", 0, verdict=ATTR_HELPFUL, occurred_at=T_PLUS_3D),
        ]
        record = aggregate_utility(events, window_days=None)["e1"]
        assert record.covered == 3, "repro_unit=trajectory 部署不按作者限计"


class TestAggregateReproduction:
    @staticmethod
    def test_authors_union_and_success_counted_per_author():
        events = [
            _repro_event("e1", AUTHOR_A, "t1", 0, outcome=OUTCOME_SUCCESS,
                         occurred_at=T0),
            _repro_event("e1", AUTHOR_B, "t2", 0, outcome=OUTCOME_SUCCESS,
                         occurred_at=T_PLUS_3D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.authors == [AUTHOR_A, AUTHOR_B], "复现过零的作者并集"
        assert record.author_success == {AUTHOR_A: 1, AUTHOR_B: 1}, "成功按作者计"
        assert record.last_positive_at == T_PLUS_3D, "成功复现更新最近正反馈"

    @staticmethod
    def test_strong_success_only_high_evidence():
        events = [
            _repro_event("e1", AUTHOR_A, "t1", 0, outcome=OUTCOME_SUCCESS,
                         evidence=EVIDENCE_EXTERNAL),
            _repro_event("e1", AUTHOR_B, "t2", 0, outcome=OUTCOME_SUCCESS,
                         evidence=EVIDENCE_MODEL_ONLY),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.strong_success == 1, "只有 external / user_feedback 级算强成功"

    @staticmethod
    def test_failed_reproduction_updates_last_used_only():
        events = [
            _repro_event("e1", AUTHOR_A, "t1", 0, outcome=OUTCOME_FAILURE,
                         occurred_at=T_PLUS_3D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.author_success == {}, "失败复现不计成功"
        assert record.last_positive_at == "", "失败复现不算正反馈"
        assert record.last_used_at == T_PLUS_3D, "复现也是一次取用"

    @staticmethod
    def test_reproduction_never_window_limited():
        events = [
            _repro_event("e1", AUTHOR_A, "t1", 0, outcome=OUTCOME_SUCCESS, occurred_at=T0),
            _repro_event("e1", AUTHOR_A, "t2", 0, outcome=OUTCOME_SUCCESS,
                         occurred_at=T_PLUS_1D),
        ]
        record = aggregate_utility(events)["e1"]
        assert record.author_success == {AUTHOR_A: 2}, "复现事件不受归因窗口限计"


class TestAggregateRedirect:
    @staticmethod
    def test_events_counted_towards_redirect_target():
        events = [
            _attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL),
            _attr_event("e2", AUTHOR_B, "t2", 0, verdict=ATTR_HELPFUL),
        ]
        stats = aggregate_utility(events, redirects={"e1": "e2"})
        assert set(stats) == {"e2"}, "被合并条目的事件计入去向条目"
        assert stats["e2"].covered == 2, "两份事件的效用都归到去向"

    @staticmethod
    def test_chained_redirect_followed_to_end():
        events = [_attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL)]
        stats = aggregate_utility(events, redirects={"e1": "e2", "e2": "e3"})
        assert set(stats) == {"e3"}, "链式重定向跟到头（A 并入 B、B 又并入 C）"

    @staticmethod
    def test_redirect_cycle_terminates():
        events = [_attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL)]
        stats = aggregate_utility(events, redirects={"e1": "e2", "e2": "e1"})
        assert set(stats) == {"e1"}, "成环时止于当前值，不死循环"


class TestAggregatePrior:
    @staticmethod
    def test_prior_fields_carried_over():
        events = [_attr_event("e1", AUTHOR_A, "t1", 0, verdict=ATTR_HELPFUL)]
        prior = {
            "e1": StatsRecord(
                merged_into="e2", competition_group="g-1", sealed={AUTHOR_A: 3}
            )
        }
        record = aggregate_utility(events, prior=prior)["e1"]
        assert record.merged_into == "e2", "合并去向从 prior 带过"
        assert record.competition_group == "g-1", "竞争组从 prior 带过"
        assert record.sealed == {AUTHOR_A: 3}, "封存计数从 prior 带过"
        record.sealed[AUTHOR_B] = 1
        assert prior["e1"].sealed == {AUTHOR_A: 3}, "带过的是副本，不回写 prior"

    @staticmethod
    def test_prior_without_events_not_in_result():
        prior = {"e9": StatsRecord(covered=5)}
        stats = aggregate_utility([], prior=prior)
        assert stats == {}, "无事件的条目不在结果里（落盘保持原记录）"


class TestComputeBaseline:
    @staticmethod
    def test_median_by_category():
        stats = {
            "e1": StatsRecord(helpful={EVIDENCE_EXTERNAL: 1}),
            "e2": StatsRecord(helpful={EVIDENCE_EXTERNAL: 3}),
            "e3": StatsRecord(harmful={EVIDENCE_EXTERNAL: 1}),
        }
        categories = {"e1": "deploy", "e2": "deploy", "e3": "config"}
        baseline = compute_baseline(stats, categories)
        # deploy: validity = (1+1)/(1+2)=2/3 与 (3+1)/(3+2)=0.8，中位数 7/10；config: 1/3。
        assert baseline["deploy"] == pytest.approx((2 / 3 + 0.8) / 2), "类别中位数"
        assert baseline["config"] == pytest.approx(1 / 3), "类别独立"

    @staticmethod
    def test_prior_baseline_feeds_validity():
        stats = {"e1": StatsRecord(helpful={EVIDENCE_EXTERNAL: 1})}
        categories = {"e1": "deploy"}
        cold = compute_baseline(stats, categories)
        warm = compute_baseline(stats, categories, prior_baseline={"deploy": 0.9})
        assert cold["deploy"] == pytest.approx(2 / 3), "无上一轮基线回退默认 0.5"
        assert warm["deploy"] == pytest.approx(2.8 / 3), "本轮有效性用上一轮基线计算"

    @staticmethod
    def test_entries_without_category_excluded():
        stats = {"e1": StatsRecord(helpful={EVIDENCE_EXTERNAL: 1})}
        assert compute_baseline(stats, {}) == {}, "无类别的条目不参与，也不产出空类别"


class TestStoreScanAndSave:
    """UtilityStore 的聚合配套：事件扫描与统计/基线写入。"""

    @staticmethod
    def test_scan_events_parses_written_events():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        store.write_attribution_event(
            SCOPE, "e1", AUTHOR_A, "t1", 0,
            AttributionEvent(verdict=ATTR_HELPFUL, author=AUTHOR_A, occurred_at=T0),
        )
        store.write_reproduction_event(
            SCOPE, "e1", AUTHOR_B, "t2", 0,
            ReproductionEvent(trajectory_id="t2", author=AUTHOR_B,
                              outcome=OUTCOME_SUCCESS, occurred_at=T_PLUS_3D),
        )
        scanned = store.scan_events(SCOPE)
        assert len(scanned) == 2, "两个事件都被扫出"
        kinds = {key.kind for key, _payload in scanned}
        assert kinds == {"attr", "repro"}, "事件键解析出类型"
        assert all(payload.get("occurred_at") for _key, payload in scanned), (
            "载荷随事件返回"
        )

    @staticmethod
    def test_scan_events_skips_malformed():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        store.write_attribution_event(
            SCOPE, "e1", AUTHOR_A, "t1", 0, AttributionEvent(verdict=ATTR_HELPFUL)
        )
        kv.insert(SCOPE, "/experience/utility/bad-key", encode_payload({"verdict": "helpful"}))
        kv.insert(SCOPE, attribution_event_key("e2", AUTHOR_A, "t1", 0), b"\xff\xfe")
        scanned = store.scan_events(SCOPE)
        assert {key.entry_id for key, _payload in scanned} == {"e1"}, (
            "坏键与坏载荷跳过，好事件照常返回（scan 顺序由实现定义，按集合比）"
        )

    @staticmethod
    def test_scan_events_empty_scope():
        assert UtilityStore(InMemoryKVStore()).scan_events(SCOPE) == [], "无事件返回空"

    @staticmethod
    def test_save_stats_roundtrip_and_overwrite():
        store = UtilityStore(InMemoryKVStore())
        record = StatsRecord(
            helpful={EVIDENCE_USER_FEEDBACK: 2}, covered=2,
            authors=[AUTHOR_A], author_success={AUTHOR_A: 1},
        )
        store.save_stats(SCOPE, "e1", record)
        assert store.load_stats(SCOPE, "e1") == record, "统计记录写后可读回"
        replacement = StatsRecord(covered=7, watermark="2026-10-01T00:00:00+00:00")
        store.save_stats(SCOPE, "e1", replacement)
        assert store.load_stats(SCOPE, "e1") == replacement, "每轮整体覆写"

    @staticmethod
    def test_save_baseline_roundtrip_and_overwrite():
        store = UtilityStore(InMemoryKVStore())
        store.save_baseline(SCOPE, {"deploy": 0.6})
        assert store.load_baseline(SCOPE) == {"deploy": 0.6}, "基线写后可读回"
        store.save_baseline(SCOPE, {"deploy": 0.7, "config": 0.4})
        assert store.load_baseline(SCOPE) == {"deploy": 0.7, "config": 0.4}, (
            "基线整体覆写"
        )

    @staticmethod
    def test_scan_aggregate_save_end_to_end():
        kv = InMemoryKVStore()
        store = UtilityStore(kv)
        store.write_attribution_event(
            SCOPE, "e1", AUTHOR_A, "t1", 0,
            AttributionEvent(verdict=ATTR_HELPFUL, evidence=EVIDENCE_EXTERNAL,
                             author=AUTHOR_A, occurred_at=T0),
        )
        stats = aggregate_utility(store.scan_events(SCOPE), watermark="2026-10-01T00:00:00+00:00")
        store.save_stats(SCOPE, "e1", stats["e1"])
        loaded = store.load_stats(SCOPE, "e1")
        assert loaded == stats["e1"], "扫描 → 聚合 → 落盘一路一致"
        assert loaded.helpful == {EVIDENCE_EXTERNAL: 1}, "事件按证据等级计入"
