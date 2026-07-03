# coding: utf-8
"""server UT —— /add_messages/ 端点（mock memory_engine 隔离 LLM）。

被测对象：端点层的消息转换、AgentMemoryConfig 构造、抽取开关透传、异常转 500。
MemVariable→Param 适配的细粒度校验见 test_mem_variables_validation.py。
"""
import pytest

from jiuwen_memory.foundation.llm import BaseMessage
from jiuwen_memory.memory_core.config.config import AgentMemoryConfig


def _msg(role="user", content="x"):
    return {"role": role, "content": content}


# ====== 1. 消息转换：BaseMessage 构造 ======
@pytest.mark.asyncio
async def test_messages_converted_to_base_message(client, mock_engine):
    """请求 messages(dict) → BaseMessage 列表，role/content 透传。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    })
    call = mock_engine.add_messages.call_args
    msgs = call.kwargs["messages"]
    assert len(msgs) == 2
    assert all(isinstance(m, BaseMessage) for m in msgs)
    assert (msgs[0].role, msgs[0].content) == ("user", "你好")
    assert (msgs[1].role, msgs[1].content) == ("assistant", "你好呀")


@pytest.mark.asyncio
async def test_message_missing_role_defaults_to_user(client, mock_engine):
    """消息缺 role 时端点用默认 'user'（端点 msg.get('role','user')）。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={"messages": [{"content": "无角色"}]})
    msgs = mock_engine.add_messages.call_args.kwargs["messages"]
    assert msgs[0].role == "user"
    assert msgs[0].content == "无角色"


@pytest.mark.asyncio
async def test_message_missing_content_defaults_empty(client, mock_engine):
    """消息缺 content 时端点用空串。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={"messages": [{"role": "user"}]})
    msgs = mock_engine.add_messages.call_args.kwargs["messages"]
    assert msgs[0].content == ""


# ====== 2. user_id / scope_id 透传 ======
@pytest.mark.asyncio
async def test_user_scope_transparent_passthrough(client, mock_engine):
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={
        "messages": [_msg()], "user_id": "u_abc", "scope_id": "sc_xyz",
    })
    call = mock_engine.add_messages.call_args
    assert call.kwargs["user_id"] == "u_abc"
    assert call.kwargs["scope_id"] == "sc_xyz"


@pytest.mark.asyncio
async def test_user_scope_default_when_omitted(client, mock_engine):
    """不传 user_id/scope_id 时用 LongTermMemory.DEFAULT_VALUE。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={"messages": [_msg()]})
    call = mock_engine.add_messages.call_args
    # DEFAULT_VALUE 是引擎定义的占位串，只断言它被透传（具体值由引擎决定）
    assert call.kwargs["user_id"] == call.kwargs["scope_id"]


# ====== 3. 抽取开关透传 ======
@pytest.mark.asyncio
async def test_extraction_switches_default_all_true(client, mock_engine):
    """不传开关时 5 个 enable_* 默认 True，对齐引擎默认。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={"messages": [_msg()]})
    cfg: AgentMemoryConfig = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.enable_long_term_mem is True
    assert cfg.enable_user_profile is True
    assert cfg.enable_semantic_memory is True
    assert cfg.enable_episodic_memory is True
    assert cfg.enable_summary_memory is True


@pytest.mark.parametrize("field", [
    "enable_long_term_mem", "enable_user_profile", "enable_semantic_memory",
    "enable_episodic_memory", "enable_summary_memory",
])
@pytest.mark.asyncio
async def test_single_switch_disabled(client, mock_engine, field):
    """单独关掉某个开关，其余保持 True。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={"messages": [_msg()], field: False})
    cfg: AgentMemoryConfig = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert getattr(cfg, field) is False
    all_switches = [
        "enable_long_term_mem", "enable_user_profile", "enable_semantic_memory",
        "enable_episodic_memory", "enable_summary_memory",
    ]
    for switch in all_switches:
        if switch == field:
            continue
        assert getattr(cfg, switch) is True


@pytest.mark.asyncio
async def test_all_switches_disabled(client, mock_engine):
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json={
        "messages": [_msg()],
        "enable_long_term_mem": False, "enable_user_profile": False,
        "enable_semantic_memory": False, "enable_episodic_memory": False,
        "enable_summary_memory": False,
    })
    cfg: AgentMemoryConfig = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert not any([cfg.enable_long_term_mem, cfg.enable_user_profile,
                    cfg.enable_semantic_memory, cfg.enable_episodic_memory,
                    cfg.enable_summary_memory])


# ====== 4. 成功响应 + 异常处理 ======
@pytest.mark.asyncio
async def test_success_response_shape(client, mock_engine):
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json={"messages": [_msg()]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert "message" in body


@pytest.mark.asyncio
async def test_engine_value_error_returns_500(client, mock_engine):
    mock_engine.add_messages.side_effect = ValueError("bad input")
    r = await client.post("/add_messages/", json={"messages": [_msg()]})
    assert r.status_code == 500
    assert "bad input" in r.json()["detail"]


@pytest.mark.asyncio
async def test_engine_runtime_error_returns_500(client, mock_engine):
    mock_engine.add_messages.side_effect = RuntimeError("engine boom")
    r = await client.post("/add_messages/", json={"messages": [_msg()]})
    assert r.status_code == 500
    assert "engine boom" in r.json()["detail"]


@pytest.mark.asyncio
async def test_engine_unexpected_exception_returns_500(client, mock_engine):
    """任意异常都被 except Exception 兜成 500，不泄漏为其它状态码。"""
    mock_engine.add_messages.side_effect = KeyError("missing")
    r = await client.post("/add_messages/", json={"messages": [_msg()]})
    assert r.status_code == 500


# ====== 5. 请求体校验（422，不碰 engine） ======
@pytest.mark.asyncio
async def test_missing_messages_rejected(client, mock_engine):
    r = await client.post("/add_messages/", json={"user_id": "u"})
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_empty_messages_list_accepted(client, mock_engine):
    """空 messages 列表是合法 list，通过校验（是否真正写入由 engine 决定）。"""
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json={"messages": []})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_message_not_dict_rejected(client, mock_engine):
    """messages 元素非 dict 应 422，且不触达 engine。"""
    r = await client.post("/add_messages/", json={"messages": ["plain string"]})
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_enable_wrong_type_rejected(client, mock_engine):
    """enable_* 传无法强转为 bool 的类型（如 list）应 422；字符串会被 pydantic 强转。"""
    r = await client.post("/add_messages/", json={
        "messages": [_msg()], "enable_long_term_mem": ["not", "bool"],
    })
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()
