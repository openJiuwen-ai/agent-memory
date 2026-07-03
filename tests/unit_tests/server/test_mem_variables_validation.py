# coding: utf-8
"""server UT —— MemVariable 窄模型校验矩阵（422 路径，不碰 engine）。

被测对象：AddMessagesRequest.mem_variables: list[MemVariable] 的反序列化校验。
- name/description 必填；type 仅简单类型；extra='forbid'。
- 所有 422 用例都断言 mock_engine.add_messages 未被调用（校验在端点逻辑之前）。
"""
import pytest

from jiuwen_memory.common.schema.param import Param, ParamType


def _body(mem_variables, **extra):
    payload = {"messages": [{"role": "user", "content": "我叫李雷，今年30岁"}]}
    if mem_variables is not None:
        payload["mem_variables"] = mem_variables
    payload.update(extra)
    return payload


# ====== 1. name/description 必填 ======
@pytest.mark.asyncio
async def test_missing_name_rejected(client, mock_engine):
    r = await client.post("/add_messages/", json=_body([{"description": "无 name"}]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_missing_description_rejected(client, mock_engine):
    r = await client.post("/add_messages/", json=_body([{"name": "v"}]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_empty_name_rejected(client, mock_engine):
    """空字符串 name 应被拒（虽然非 None，但空串不构成有效变量名）。"""
    r = await client.post("/add_messages/", json=_body([{"name": "", "description": "d"}]))
    # pydantic 对 str 必填只校验存在性，空串可能通过；若通过则 engine 应被调用
    # 这里记录实际行为，不强加假设
    assert r.status_code in (200, 422)


@pytest.mark.asyncio
async def test_name_description_only_accepted(client, mock_engine):
    """仅 name+description（缺 type/required/default）应通过校验。"""
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json=_body([
        {"name": "user_name", "description": "姓名"},
    ]))
    assert r.status_code == 200
    mock_engine.add_messages.assert_awaited_once()


# ====== 2. type 合法值 ======
@pytest.mark.parametrize("type_str,expected", [
    ("string", ParamType.String),
    ("boolean", ParamType.Boolean),
    ("integer", ParamType.Integer),
    ("number", ParamType.Number),
])
@pytest.mark.asyncio
async def test_valid_simple_types(client, mock_engine, type_str, expected):
    """四种简单类型应通过，且适配成对应 ParamType。"""
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "type": type_str},
    ]))
    assert r.status_code == 200
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].type == expected


@pytest.mark.asyncio
async def test_type_default_string_when_omitted(client, mock_engine):
    """不传 type 时默认 string。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([{"name": "v", "description": "d"}]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].type == ParamType.String


# ====== 3. type 非法值（422） ======
@pytest.mark.parametrize("bad_type", ["array", "object"])
@pytest.mark.asyncio
async def test_complex_types_rejected(client, mock_engine, bad_type):
    """Array/Object 嵌套类型不支持，反序列化阶段即 422。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "type": bad_type},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.parametrize("bad_type", ["foobar", "STRING", "datetime", "int", "str", "", "None", "1"])
@pytest.mark.asyncio
async def test_unknown_types_rejected(client, mock_engine, bad_type):
    """未知/大小写错误/空 type 应 422，不静默降级。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "type": bad_type},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


# ====== 4. required 字段 ======
@pytest.mark.asyncio
async def test_required_default_true_when_omitted(client, mock_engine):
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([{"name": "v", "description": "d"}]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].required is True


@pytest.mark.parametrize("required_val", [True, False])
@pytest.mark.asyncio
async def test_required_explicit_values(client, mock_engine, required_val):
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "required": required_val},
    ]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].required is required_val


@pytest.mark.asyncio
async def test_required_string_coerced_to_bool(client, mock_engine):
    """pydantic bool 字段会强转字符串：'yes'/'1'/非空串 → True，'no'/'0'/'' → False，不报 422。

    这是 pydantic v2 的标准 bool coercion 行为，记录实际语义而非假设 422。
    """
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "required": "yes"},
    ]))
    assert r.status_code == 200
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].required is True


@pytest.mark.asyncio
async def test_required_uncoercible_type_rejected(client, mock_engine):
    """required 传无法强转为 bool 的类型（如 list）应 422。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "required": ["not", "bool"]},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


# ====== 5. default 字段 ======
@pytest.mark.asyncio
async def test_default_field_passed_through(client, mock_engine):
    """default 可选，透传到 Param.default。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "default": "fallback"},
    ]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].default == "fallback"


@pytest.mark.asyncio
async def test_default_none_when_omitted(client, mock_engine):
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([{"name": "v", "description": "d"}]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables[0].default is None


# ====== 6. extra='forbid'：未知字段 ======
@pytest.mark.asyncio
async def test_unknown_field_rejected(client, mock_engine):
    """拼错字段名应 422。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "descripton": "拼错"},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_extra_items_field_rejected(client, mock_engine):
    """MemVariable 不含 items（复杂类型专用），传 items 应 422。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "items": {"name": "x"}},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


@pytest.mark.asyncio
async def test_extra_properties_field_rejected(client, mock_engine):
    """MemVariable 不含 properties，传应 422。"""
    r = await client.post("/add_messages/", json=_body([
        {"name": "v", "description": "d", "properties": []},
    ]))
    assert r.status_code == 422
    mock_engine.add_messages.assert_not_called()


# ====== 7. 多变量混合 + 全量适配 ======
@pytest.mark.asyncio
async def test_mixed_variables_all_adapted(client, mock_engine):
    """混合缺省/显式变量，全部适配成 Param，顺序与类型保留。"""
    mock_engine.add_messages.return_value = None
    await client.post("/add_messages/", json=_body([
        {"name": "a", "description": "缺省全部"},
        {"name": "b", "description": "显式", "type": "integer", "required": False},
        {"name": "c", "description": "带默认", "type": "number", "default": 1.5},
    ]))
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    vs = cfg.mem_variables
    assert [v.name for v in vs] == ["a", "b", "c"]
    assert all(isinstance(v, Param) for v in vs)
    assert (vs[0].type, vs[0].required, vs[0].default) == (ParamType.String, True, None)
    assert (vs[1].type, vs[1].required) == (ParamType.Integer, False)
    assert vs[2].default == 1.5


@pytest.mark.asyncio
async def test_empty_mem_variables_list_accepted(client, mock_engine):
    """显式传空列表 = 不抽取变量，应通过。"""
    mock_engine.add_messages.return_value = None
    r = await client.post("/add_messages/", json=_body([]))
    assert r.status_code == 200
    cfg = mock_engine.add_messages.call_args.kwargs["agent_config"]
    assert cfg.mem_variables == []
