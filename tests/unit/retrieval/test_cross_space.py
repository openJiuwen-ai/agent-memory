"""跨空间取数上界、结果合并与失败编码的纯单测（`retrieval/cross_space.py`）。

三个函数覆盖同一次跨空间调用的三个环节，都不访问存储、不调模型，因此脱离装配单测。
用例原先落在 ``tests/unit/api/test_collective_routing.py``——那时 API 层直接调用这三个
函数。扇出编排下沉到 ``control/collective/cross_space_recall.py`` 之后调用方换成控制层，
用例随源码路径迁到本文件（AGENTS.md「单测路径镜像源码」）。API 侧只留经装配的集成用例。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import RecallChannel
from jiuwen_memory.control import collective
from jiuwen_memory.retrieval import cross_space
from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem

pytestmark = pytest.mark.unit

ALICE_SPACE = "u_alice"
PROJECT_SPACE = "p_apollo"


def _result(contents: list[str]) -> RetrievalResult:
    return RetrievalResult(
        items=[RetrievedItem(unit_id=f"id-{item}", content=item) for item in contents]
    )


# -- 取数上界 --------------------------------------------------------------- #


def test_each_space_fetches_up_to_top_k_rather_than_a_fixed_share() -> None:
    """取数是上界不是定额：定额的缺口不回流，表现为静默少返回。"""
    assert cross_space.allocate_quota([ALICE_SPACE, PROJECT_SPACE], 10) == {
        ALICE_SPACE: 10,
        PROJECT_SPACE: 10,
    }


def test_the_total_fetch_is_capped_when_top_k_is_large() -> None:
    """空间数上限封住空间数，取数总量上限封住 top_k 很大的调用。"""
    spaces = [f"s{index}" for index in range(collective.SPACE_FANOUT_LIMIT)]
    quota = cross_space.allocate_quota(spaces, 1000)
    assert sum(quota.values()) == cross_space.TOTAL_FETCH_CAP


# -- 轮转合并 --------------------------------------------------------------- #


def test_a_space_that_runs_out_hands_its_slots_to_the_others() -> None:
    """轮转的第一处收益：队列取空即本轮跳过，未用完的名额流给仍有内容的空间。

    定额分配下这些名额空置——实测 20 条 + 1 条、top_k=10 时只返回 6 条。
    """
    merged = cross_space.merge(
        [
            (ALICE_SPACE, _result([f"a{index}" for index in range(10)])),
            (PROJECT_SPACE, _result(["b0", "b1", "b2"])),
        ],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert len(merged.items) == 10
    assert [item.content for item in merged.items[:6]] == ["a0", "b0", "a1", "b1", "a2", "b2"]


def test_a_duplicate_consumes_the_queue_but_not_a_slot() -> None:
    """轮转的第二处收益：重复内容不占 top_k 名额，从同一队列继续向下取。"""
    shared = ["x1", "x2", "x3"]
    merged = cross_space.merge(
        [
            (ALICE_SPACE, _result([*shared, *(f"a{index}" for index in range(4, 11))])),
            (PROJECT_SPACE, _result([*shared, *(f"b{index}" for index in range(4, 11))])),
        ],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert len(merged.items) == 10
    assert [item.content for item in merged.items].count("x1") == 1


def test_every_space_with_content_gets_a_slot_before_any_space_gets_a_second_round() -> None:
    """轮转保证的是覆盖面：靠前的空间不会独占 top_k。

    顺序拼接下 ``PROJECT_SPACE`` 整个不出现——各空间取 top_k 之后首个空间即填满。
    """
    merged = cross_space.merge(
        [
            (ALICE_SPACE, _result([f"a{index}" for index in range(10)])),
            (PROJECT_SPACE, _result([f"b{index}" for index in range(10)])),
        ],
        top_k=4,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert [item.content for item in merged.items] == ["a0", "b0", "a1", "b1"]


def test_merging_stops_when_every_remaining_item_is_a_duplicate() -> None:
    """全部队列只剩重复内容时停止，不空转。"""
    merged = cross_space.merge(
        [(ALICE_SPACE, _result(["x", "y"])), (PROJECT_SPACE, _result(["x", "y"]))],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert [item.content for item in merged.items] == ["x", "y"]


def test_a_failed_space_keeps_its_channel_errors_in_the_merged_result() -> None:
    """轮转不改变 errors 的归并：某个空间的通道失败不在跨空间调用里消失。"""
    failed = RetrievalResult()
    failed.errors.append("channel down")
    merged = cross_space.merge(
        [(ALICE_SPACE, _result(["a0"])), (PROJECT_SPACE, failed)],
        top_k=10,
        priority=[ALICE_SPACE, PROJECT_SPACE],
    )
    assert merged.errors == ["channel down"]


# -- 空间级失败的编码 ------------------------------------------------------- #


def test_a_space_level_failure_is_encoded_on_the_space_channel() -> None:
    """``RecallChannel.SPACE`` 是「某个空间整体没进结果」的标记位，不是召回通道。

    判权剔除在 API 层、扇出失败在控制层，两处共用本构造点——各写一份即 ``channel`` /
    ``source`` 的编码在两层各有一份，改一处漏一处的表现是调用方按 ``source`` 分不出是
    哪个空间，且不报错。
    """
    error = cross_space.space_error(PROJECT_SPACE, "PermissionDeniedError", "read denied")

    assert error.channel is RecallChannel.SPACE
    assert error.source == PROJECT_SPACE
    assert error.error_type == "PermissionDeniedError"
    assert error.message == "read denied"
