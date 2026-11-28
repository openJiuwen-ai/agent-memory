"""``LLMRouter`` 的响应解析、缺判回落与重试（群体记忆 F07「归属判定算子」）。

本文件测的是模型实现内部的三段无覆盖分支，判定表解析与两个落盘不变量在
``test_router_table.py``、接线在 ``tests/unit/api/test_collective_routing.py``。

这三段的失效方向一致且都不报错：解析失败或单条缺判时条目默默落 fallback 空间，从调用侧
看是写入成功、内容也读得到，只是落点不对。集成用例断言不到——判定桩不经这些分支。
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import HealthCheckError, ValidationError
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import ChatMessage, MemoryUnit, Scope, Segment
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.common import strip_batch_reference_shell
from jiuwen_memory.construction.router import RouteContext, RouteTable, parse_route_table
from jiuwen_memory.construction.router_impl.llm_router import (
    LLMRouter,
    _parse_response,
)

pytestmark = pytest.mark.unit

ORG = "acme"

TABLE_CONFIG = {
    "coord_entities": ["project"],
    "memory_classes": [
        {
            "name": "user_profile",
            "owner": "user",
            "space_template": "u_{user}",
            "fallback": True,
            "description": "facts about the user",
        },
        {
            "name": "long_term_memory",
            "owner": "project",
            "space_template": "p_{project}",
            "cross_user": True,
            "members": "project participants",
        },
    ],
    "narrow_dims": [
        {"entity": "agent", "tag_key": "agent_id"},
        {"entity": "session", "tag_key": "session_id"},
    ],
}


class _ScriptedLLM(LLM):
    """按脚本逐次作答的假 LLM：字符串直接返回，异常实例则抛出。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        self.calls.append(messages)
        reply = self._responses.pop(0) if self._responses else "[]"
        if isinstance(reply, Exception):
            raise reply
        return str(reply)

    def health(self) -> None:
        return None


class _UnhealthyLLM(_ScriptedLLM):
    def health(self) -> None:
        raise RuntimeError("backend down")


def _table() -> RouteTable:
    return parse_route_table(dict(TABLE_CONFIG))


def _ctx(table: RouteTable, **coords) -> RouteContext:
    resolved = {"user": "alice", "agent": "a1", "session": "s1", **coords}
    spaces = table.naming.spaces(resolved)
    return RouteContext(
        coords=resolved,
        candidates=tuple(Scope(org=ORG, space=space) for space in spaces.values()),
        fallback=Scope(org=ORG, space=table.naming.fallback_space(resolved)),
        classes=table.classes,
        narrow_dims=table.narrow_dims,
    )


def _unit(content: str) -> MemoryUnit:
    """构造带内部 id 的单元；prompt 绑定只使用批内数字引用。"""
    return MemoryUnit(id=str(uuid.uuid4()), segments=[Segment(content=content)])


def _router(*responses: object, retries: int = 3) -> tuple[LLMRouter, _ScriptedLLM]:
    llm = _ScriptedLLM(*responses)
    return LLMRouter(llm, _table(), max_attempts=retries, retry_backoff_ms=0), llm


def _reply(memory_class: str = "user_profile", **extra: object) -> str:
    return json.dumps([{"source_id": 1, "memory_class": memory_class, **extra}])


# -- 响应解析 -------------------------------------------------------------- #


def test_a_plain_json_array_parses() -> None:
    assert _parse_response('[{"source_id": "u1"}]') == [{"source_id": "u1"}]


@pytest.mark.parametrize(
    "fenced",
    [
        '```json\n[{"source_id": "u1"}]\n```',
        '```\n[{"source_id": "u1"}]\n```',
    ],
)
def test_markdown_fences_are_tolerated(fenced: str) -> None:
    """模型常把 JSON 包在围栏里，且围栏语言标记有无都出现过。"""
    assert _parse_response(fenced) == [{"source_id": "u1"}]


def test_a_single_object_is_wrapped_into_a_list() -> None:
    """单条输入时模型常直接回一个对象而不是单元素数组。"""
    assert _parse_response('{"source_id": "u1"}') == [{"source_id": "u1"}]


@pytest.mark.parametrize("bad", ["not json at all", "", "[{"])
def test_unparsable_output_raises(bad: str) -> None:
    """解析失败上抛，由 ``route_batch`` 统一按「全批落 fallback」处置。

    在本实现内部吞掉即降级逻辑有两份，两份迟早分叉。
    """
    with pytest.raises(ValueError, match="无法解析判定输出"):
        _parse_response(bad)


@pytest.mark.parametrize("bad", ["42", '"a string"', "true"])
def test_a_non_array_non_object_output_raises(bad: str) -> None:
    with pytest.raises(ValueError, match="应是 JSON 数组"):
        _parse_response(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[ID: abc]", "abc"),
        ("  [ID: abc]  ", "abc"),
        ("abc", "abc"),
        ("ID: abc", "ID: abc"),
    ],
)
def test_the_id_shell_is_stripped_before_matching(raw: str, expected: str) -> None:
    """模型把 ``[ID: ...]`` 壳连内容回传时统一去壳。

    与分类/抽取共用 ``construction.common`` 的同一去壳实现；不带方括号的裸
    回显不算壳，留在引用里按协议违规处理，不猜测。
    """
    assert strip_batch_reference_shell(raw) == expected


def test_router_rejects_invalid_batch_references(caplog) -> None:
    """布尔、零、前导零、真实 UUID 是序号协议违规；越界只走缺判回落。

    批内序号协议是 ``[1-9]\\d*``：把校验放宽成 ``\\d+`` 时，``0``/``01`` 会落进
    「越界」分支（无告警），本用例的违规告警计数随之失败。
    """
    true_uuid = str(uuid.uuid4())
    router, _llm = _router(json.dumps([
        {"source_id": 1, "memory_class": "long_term_memory"},
        {"source_id": 0, "memory_class": "user_profile"},
        {"source_id": "01", "memory_class": "user_profile"},
        {"source_id": true_uuid, "memory_class": "user_profile"},
        {"source_id": True, "memory_class": "user_profile"},
        {"source_id": 3, "memory_class": "user_profile"},
    ]))
    ctx = _ctx(_table(), project="p1")
    first, second = _unit("项目 p1 部署在集群 A"), _unit("我偏好深色主题")

    component_logger = logging.getLogger("agent_memory")
    old_propagate = component_logger.propagate
    component_logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING):
            decisions = router.route([first, second], ctx)
    finally:
        component_logger.propagate = old_propagate

    assert decisions[0].memory_class == "long_term_memory"
    assert decisions[0].scope.space == "p_p1"
    assert decisions[1].reason == "no decision for unit"
    invalid_warnings = [
        record
        for record in caplog.records
        if "invalid batch reference" in record.getMessage()
    ]
    assert len(invalid_warnings) == 4, "0/01/UUID/布尔违规告警；越界 3 只缺判回落"


# -- 判定结论的折算 -------------------------------------------------------- #


def test_a_routed_unit_takes_its_class_and_narrow_hits() -> None:
    table = _table()
    ctx = _ctx(table, project="p1")
    unit = _unit("项目 p1 部署在集群 A")
    router, _llm = _router(
        _reply("long_term_memory", narrow={"agent_id": True, "session_id": False})
    )
    (decision,) = router.route([unit], ctx)
    assert decision.memory_class == "long_term_memory"
    assert decision.scope.space == "p_p1"
    # 只含判为真的收窄维。判为否的键补空串是落盘不变量，由契约层的 ``route_batch``
    # 施加而不在本实现内——放实现内则换一个实现即可能漏掉。
    assert decision.tags == {"agent_id": "a1"}


def test_prompt_uses_batch_number_instead_of_internal_id() -> None:
    table = _table()
    internal_id = "11111111-1111-1111-1111-111111111111"
    unit = MemoryUnit(
        id=internal_id,
        segments=[Segment(content="项目 p1 部署在集群 A")],
    )
    router, llm = _router(_reply("long_term_memory"))

    router.route([unit], _ctx(table, project="p1"))

    system_text = llm.calls[0][0].content
    user_text = llm.calls[0][1].content
    assert "[ID: 1]" in user_text
    assert internal_id not in system_text + user_text
    assert '"source_id": 1' in system_text


def test_an_id_returned_inside_its_shell_still_matches() -> None:
    """模型把批内数字引用连壳回传时仍要匹配上。"""
    table = _table()
    ctx = _ctx(table, project="p1")
    unit = _unit("项目 p1 部署在集群 A")
    router, _llm = _router(
        '[{"source_id": "[ID: 1]", "memory_class": "long_term_memory"}]'
    )
    (decision,) = router.route([unit], ctx)
    assert decision.scope.space == "p_p1"


def test_a_unit_missing_from_the_response_falls_back_alone() -> None:
    """单条缺判只让这一条落 fallback，不牵连同批其余条目。

    整批一起落 fallback 会把一次模型漏答放大成整次交互的判定失效。
    """
    table = _table()
    ctx = _ctx(table, project="p1")
    routed, missing = _unit("项目 p1 部署在集群 A"), _unit("我偏好深色主题")
    router, _llm = _router(_reply("long_term_memory", narrow={}))
    decisions = router.route([routed, missing], ctx)
    assert [d.scope.space for d in decisions] == ["p_p1", "u_alice"]
    assert decisions[1].reason == "no decision for unit"


def test_blank_internal_ids_do_not_affect_reference_matching() -> None:
    """内部 id 缺失不进入 prompt；数字引用仍逐条绑定到输入顺序。"""
    table = _table()
    ctx = _ctx(table, project="p1")
    blank_a = MemoryUnit(segments=[Segment(content="我偏好深色主题")])
    blank_b = MemoryUnit(segments=[Segment(content="项目 p1 部署在集群 A")])
    router, _llm = _router(
        json.dumps(
            [
                {"source_id": 1, "memory_class": "user_profile", "narrow": {}},
                {"source_id": 2, "memory_class": "long_term_memory", "narrow": {}},
            ]
        )
    )
    decisions = router.route([blank_a, blank_b], ctx)
    assert [d.scope.space for d in decisions] == ["u_alice", "p_p1"]


def test_duplicate_internal_ids_do_not_affect_reference_matching() -> None:
    """内部 id 重复不进入 prompt；数字引用仍能区分两个输入位置。"""
    table = _table()
    ctx = _ctx(table, project="p1")
    same = str(uuid.uuid4())
    dup_a = MemoryUnit(id=same, segments=[Segment(content="我偏好深色主题")])
    dup_b = MemoryUnit(id=same, segments=[Segment(content="项目 p1 部署在集群 A")])
    router, _llm = _router(
        json.dumps(
            [
                {"source_id": 1, "memory_class": "user_profile", "narrow": {}},
                {"source_id": 2, "memory_class": "long_term_memory", "narrow": {}},
            ]
        )
    )
    decisions = router.route([dup_a, dup_b], ctx)
    assert [d.scope.space for d in decisions] == ["u_alice", "p_p1"]


def test_a_missing_reference_falls_back_when_the_response_is_short() -> None:
    """响应缺少第二个数字引用时，只让对应条目落 fallback。"""
    table = _table()
    ctx = _ctx(table, project="p1")
    blank_a = MemoryUnit(segments=[Segment(content="项目 p1 部署在集群 A")])
    blank_b = MemoryUnit(segments=[Segment(content="我偏好深色主题")])
    router, _llm = _router(
        json.dumps([{"source_id": 1, "memory_class": "long_term_memory", "narrow": {}}])
    )
    decisions = router.route([blank_a, blank_b], ctx)
    assert [d.scope.space for d in decisions] == ["p_p1", "u_alice"]
    assert decisions[1].reason == "no decision for unit"


def test_a_non_dict_narrow_value_yields_no_hits() -> None:
    """``narrow`` 不是对象时按「全部判否」处置，不抛。

    模型把该字段回成字符串或数组都出现过。抛出会让整批走「全批落 fallback」，而实际只是
    收窄维这一项无从取值，类别判断本身仍有效。
    """
    table = _table()
    ctx = _ctx(table)
    unit = _unit("我偏好深色主题")
    router, _llm = _router(_reply(narrow="yes"))
    (decision,) = router.route([unit], ctx)
    assert decision.tags == {}
    assert decision.memory_class == "user_profile"


def test_a_non_dict_entry_in_the_response_is_skipped() -> None:
    """数组里混入非对象项时跳过该项，其余照常折算。"""
    table = _table()
    ctx = _ctx(table)
    unit = _unit("我偏好深色主题")
    router, _llm = _router(
        '["garbage", {"source_id": 1, "memory_class": "user_profile"}]'
    )
    (decision,) = router.route([unit], ctx)
    assert decision.memory_class == "user_profile"


def test_the_discard_flag_is_carried_through() -> None:
    table = _table()
    ctx = _ctx(table)
    unit = _unit("嗯")
    router, _llm = _router(_reply(discard=True))
    (decision,) = router.route([unit], ctx)
    assert decision.discarded is True


# -- 批次与重试 ------------------------------------------------------------ #


def test_no_units_makes_no_llm_call() -> None:
    router, llm = _router()
    assert router.route([], _ctx(_table())) == []
    assert llm.calls == []


def test_a_batch_over_the_size_limit_is_split_and_order_is_preserved() -> None:
    """超过单批上限时分多次调用，返回次序仍与输入一致。

    次序错位不报错，表现为条目按别人的判定落点。
    """
    table = _table()
    ctx = _ctx(table)
    units = [_unit(f"第 {index} 条") for index in range(12)]
    replies = [
        json.dumps(
            [
                {"source_id": index, "memory_class": "user_profile"}
                for index in range(1, len(units[start:start + 10]) + 1)
            ]
        )
        for start in (0, 10)
    ]
    router, llm = _router(*replies)
    decisions = router.route(units, ctx)
    assert len(llm.calls) == 2
    assert [d.unit.id for d in decisions] == [unit.id for unit in units]


def test_a_transient_llm_failure_is_retried() -> None:
    table = _table()
    ctx = _ctx(table)
    unit = _unit("我偏好深色主题")
    router, llm = _router(RuntimeError("timeout"), _reply())
    (decision,) = router.route([unit], ctx)
    assert decision.memory_class == "user_profile"
    assert len(llm.calls) == 2


def test_the_last_failure_is_raised_after_the_retries_are_used_up() -> None:
    """重试用尽后上抛原异常，不吞成空结论。"""
    table = _table()
    ctx = _ctx(table)
    router, llm = _router(*[RuntimeError("down")] * 3)
    with pytest.raises(RuntimeError, match="down"):
        router.route([_unit("我偏好深色主题")], ctx)
    assert len(llm.calls) == 3


def test_a_zero_attempt_budget_is_a_configuration_error() -> None:
    """``max_attempts=0`` 时在构造期失败。"""
    with pytest.raises(ValidationError, match="max_attempts"):
        LLMRouter(_ScriptedLLM(), _table(), max_attempts=0, retry_backoff_ms=0)


@pytest.mark.parametrize("value", [-1, True, 1.5, "bad"])
def test_invalid_attempt_budget_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError, match="max_attempts"):
        LLMRouter(_ScriptedLLM(), _table(), max_attempts=value, retry_backoff_ms=0)


@pytest.mark.parametrize("value", [-1, True, 1.5, "bad"])
def test_invalid_backoff_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError, match="retry_backoff_ms"):
        LLMRouter(_ScriptedLLM(), _table(), max_attempts=1, retry_backoff_ms=value)


# -- 算子契约 -------------------------------------------------------------- #


def test_the_operator_type_and_table_are_exposed() -> None:
    router, _llm = _router()
    assert router.operator_type() is OperatorType.ROUTER
    # 收窄维标签键各带一个归属未决派生键。
    assert router.table.tag_keys == frozenset(
        {"agent_id", "session_id", "agent_id_unresolved", "session_id_unresolved"}
    )


def test_an_unhealthy_backend_surfaces_as_a_health_check_error() -> None:
    router = LLMRouter(_UnhealthyLLM(), _table())
    with pytest.raises(HealthCheckError, match="backend down"):
        router.health()


def test_the_system_prompt_carries_the_classes_and_dimensions() -> None:
    """判定表的类别与收窄维必须进 prompt：漏掉即模型按空清单作答，全批落 fallback。"""
    table = _table()
    unit = _unit("我偏好深色主题")
    router, llm = _router(_reply())
    router.route([unit], _ctx(table))
    prompt = llm.calls[0][0].content
    assert "user_profile (FALLBACK)" in prompt
    assert "long_term_memory" in prompt
    assert "agent_id" in prompt and "session_id" in prompt


def test_the_system_prompt_carries_ownership_coordinates() -> None:
    """归属坐标必须进 prompt：模型据此决定 project/user/team 归属（F08 §1.2 复用 coords）。

    漏掉坐标即模型只按收窄维猜归属——project 级事实可能落 user_profile，召回按 project
    隔离时丢失。
    """
    table = _table()
    ctx = _ctx(table, project="p1")
    unit = _unit("项目 p1 部署在集群 A")
    router, llm = _router(_reply())
    router.route([unit], ctx)
    prompt = llm.calls[0][0].content
    assert "OWNERSHIP COORDINATES" in prompt
    assert "project=p1" in prompt
    assert "user=alice" in prompt


def test_the_system_prompt_coordinates_default_to_none() -> None:
    """无坐标时显示 ``(none)``，不因缺项把 prompt 格式化炸掉。"""
    table = _table()
    ctx = _ctx(table, user="", agent="", session="")
    unit = _unit("我偏好深色主题")
    router, llm = _router(_reply())
    router.route([unit], ctx)
    prompt = llm.calls[0][0].content
    assert "OWNERSHIP COORDINATES" in prompt
