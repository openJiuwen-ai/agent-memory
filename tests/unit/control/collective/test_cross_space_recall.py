"""跨空间召回扇出的纯单测（`control/collective/cross_space_recall.py`）。

本模块不持有引擎、不读 ``identity``、不做任何裁决，因此可脱离装配单测——召回经回调注入，
用例给一个记录入参的假回调即可覆盖全部行为。这正是把第 3—5 步下沉的直接收益：同样的行为
在 API 层只能经 ``build_kernel`` 装配出完整内核之后才测得到。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from jiuwen_memory.common.type_def import FilterClause, FilterOp, Scope, iter_clauses
from jiuwen_memory.control import collective
from jiuwen_memory.retrieval.cross_space import TOTAL_FETCH_CAP
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, RetrievedItem

pytestmark = pytest.mark.unit

ORG = "acme"
ALICE_SPACE = "u_alice"
PROJECT_SPACE = "p_apollo"


def _target(space: str, *clauses: FilterClause) -> collective.SpaceRecallTarget:
    return collective.SpaceRecallTarget(scope=Scope(org=ORG, space=space), clauses=clauses)


def _result(*contents: str) -> RetrievalResult:
    return RetrievalResult(
        items=[RetrievedItem(unit_id=f"id-{item}", content=item) for item in contents]
    )


def _recorder(per_space: dict[str, RetrievalResult]):
    """记录每空间实际收到的查询对象，返回预置结果。"""
    seen: list[tuple[Scope, RetrievalQuery]] = []

    async def recall(scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        seen.append((scope, query))
        return per_space[scope.space]

    return recall, seen


def _run(targets, query=None, *, top_k=10, recall):
    return asyncio.run(
        collective.recall_spaces(
            targets, query or RetrievalQuery(text="部署在哪"), top_k=top_k, recall=recall
        )
    )


def test_an_empty_target_set_does_not_call_recall_at_all() -> None:
    """没有可读空间时不发起任何召回：空集合走扇出等于白付一次 gather 的编排开销。"""
    calls: list[Scope] = []

    async def recall(scope: Scope, _query: RetrievalQuery) -> RetrievalResult:
        calls.append(scope)
        return RetrievalResult()

    merged, failures = _run([], recall=recall)

    assert calls == []
    assert merged.items == []
    assert failures == []


def test_each_space_is_recalled_with_its_own_fetch_ceiling() -> None:
    """取数上界摊配到逐空间的 ``top_k``，上界是 ``min(top_k, 总量上限 ÷ 空间数)``。"""
    recall, seen = _recorder({ALICE_SPACE: _result("a"), PROJECT_SPACE: _result("b")})

    _run([_target(ALICE_SPACE), _target(PROJECT_SPACE)], top_k=10, recall=recall)

    assert [query.top_k for _scope, query in seen] == [10, 10]


def test_a_very_large_top_k_is_capped_by_the_total_fetch_cap() -> None:
    """``top_k`` 传得很大时由取数总量上限封顶，不让 N 个空间同时全量取数。"""
    spaces = [f"s{index}" for index in range(4)]
    recall, seen = _recorder({space: RetrievalResult() for space in spaces})

    _run([_target(space) for space in spaces], top_k=1000, recall=recall)

    assert sum(query.top_k for _scope, query in seen) == TOTAL_FETCH_CAP


def test_each_space_gets_its_own_clauses_merged_into_the_caller_expression() -> None:
    """逐空间谓词与调用方表达式合成一个 AND 下推，各空间只拿到自己那份。

    共用一份即某个空间按另一个空间的授权取数——各空间的授权可以来自不同的策略。
    """
    caller = FilterClause("system_metadata.tier", FilterOp.EQ, "hot")
    alice_only = FilterClause("system_metadata.author_principal", FilterOp.EQ, "user:alice")
    project_only = FilterClause("system_metadata.project_id", FilterOp.IN, ["", "p1"])
    recall, seen = _recorder({ALICE_SPACE: RetrievalResult(), PROJECT_SPACE: RetrievalResult()})

    _run(
        [_target(ALICE_SPACE, alice_only), _target(PROJECT_SPACE, project_only)],
        RetrievalQuery(text="部署在哪", filters=[caller]),
        recall=recall,
    )

    fields = {
        scope.space: {clause.field for clause in iter_clauses(query.filters)}
        for scope, query in seen
    }
    assert fields[ALICE_SPACE] == {caller.field, alice_only.field}
    assert fields[PROJECT_SPACE] == {caller.field, project_only.field}


def test_a_space_without_clauses_still_carries_the_caller_expression() -> None:
    """未装配空间治理时逐空间谓词为空，调用方自己的表达式照常下推。"""
    caller = FilterClause("system_metadata.tier", FilterOp.EQ, "hot")
    recall, seen = _recorder({ALICE_SPACE: RetrievalResult()})

    _run(
        [_target(ALICE_SPACE)], RetrievalQuery(text="部署在哪", filters=[caller]), recall=recall
    )

    assert [clause.field for clause in iter_clauses(seen[0][1].filters)] == [caller.field]


def test_each_space_gets_its_own_copy_of_the_extensions_bag() -> None:
    """``extensions`` 逐空间取副本，不共用骨架里的那一份。

    它顺 parser 透传给自定义检索模块，共用一份时任何一个空间的检索模块就地改写都会
    波及其余空间。
    """
    recall, seen = _recorder({ALICE_SPACE: RetrievalResult(), PROJECT_SPACE: RetrievalResult()})
    skeleton = RetrievalQuery(text="部署在哪", extensions={"parser": "custom"})

    _run([_target(ALICE_SPACE), _target(PROJECT_SPACE)], skeleton, recall=recall)

    bags = [id(query.extensions) for _scope, query in seen]
    assert len(set(bags)) == 2, "两个空间共用了同一个 extensions 字典"
    assert id(skeleton.extensions) not in bags, "骨架自己的字典被逐空间复用"


def test_one_failing_space_does_not_fail_the_whole_call() -> None:
    """单空间失败不使整次调用失败：跨空间的语义是「在能读的空间里找」。"""

    async def recall(scope: Scope, _query: RetrievalQuery) -> RetrievalResult:
        if scope.space == PROJECT_SPACE:
            raise RuntimeError("backend down")
        return _result("a0")

    merged, failures = _run([_target(ALICE_SPACE), _target(PROJECT_SPACE)], recall=recall)

    assert [item.content for item in merged.items] == ["a0"]
    assert [(error.source, error.error_type) for error in failures] == [
        (PROJECT_SPACE, "RuntimeError")
    ]
    assert failures[0].channel.value == "space"


def test_a_fan_out_failure_is_returned_apart_from_the_merged_errors() -> None:
    """扇出失败单独返回，不并进 ``merged.errors``。

    并进去之后它与检索层的分通道错误混在同一个列表里，API 层要为「整个空间挂了」写审计
    就只能按 ``channel is SPACE`` 过滤——那是把审计判据绑在本模块的 channel 编码上。
    并进返回值是 API 层的动作，本模块只负责把两路分开交出去。
    """
    channel_error = "vector channel down"
    healthy = _result("a0")
    healthy.errors.append(channel_error)

    async def recall(scope: Scope, _query: RetrievalQuery) -> RetrievalResult:
        if scope.space == PROJECT_SPACE:
            raise RuntimeError("backend down")
        return healthy

    merged, failures = _run([_target(ALICE_SPACE), _target(PROJECT_SPACE)], recall=recall)

    # 各空间自己的分通道错误照常并入合并结果——那是 merge 的既有职责。
    assert merged.errors == [channel_error]
    # 空间级扇出失败只在分离的那一路里，没有混进去。
    assert [error.source for error in failures] == [PROJECT_SPACE]


def test_the_target_order_is_the_merge_priority() -> None:
    """``targets`` 的顺序即合并优先级：重复内容保留靠前空间的那条，轮转也按此序。"""
    recall, _seen = _recorder(
        {ALICE_SPACE: _result("shared", "a1"), PROJECT_SPACE: _result("shared", "b1")}
    )

    merged, _failures = _run(
        [_target(PROJECT_SPACE), _target(ALICE_SPACE)], top_k=10, recall=recall
    )

    assert [item.unit_id for item in merged.items] == ["id-shared", "id-a1", "id-b1"]


def test_the_module_takes_no_identity() -> None:
    """签名上不收 ``identity``（S03 不变量 22 / S02 不变量 2）。

    裁决留 PEP、裁决之后的机械换算与 I/O 编排落控制层，两者之间只经回调与成品数据衔接。
    本条钉的是签名——一旦某次改动为了方便把 ``identity`` 传进来，这里立刻失败。
    """
    params = set(inspect.signature(collective.recall_spaces).parameters)
    assert "identity" not in params
    assert params == {"targets", "query", "top_k", "recall"}
