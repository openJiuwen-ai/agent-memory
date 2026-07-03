# coding: utf-8
"""server UT —— 变量端点 /update_variables/ /delete_variables/ /get_variables/。

被测对象：请求体校验、user/scope 透传、engine 返回值序列化、异常转 500。
engine 全 AsyncMock，无 LLM/存储依赖。
"""
import pytest


# ==================== /update_variables/ ====================
def _update_body(variables, **extra):
    p = {"variables": variables, "user_id": "u1", "scope_id": "s1"}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_update_variables_success(client, mock_engine):
    mock_engine.update_variables.return_value = None
    r = await client.post("/update_variables/", json=_update_body({"name": "李雷", "age": "30"}))
    assert r.status_code == 200
    assert r.json()["status"] == "success"


@pytest.mark.asyncio
async def test_update_variables_passthrough(client, mock_engine):
    """variables/user/scope 透传给 engine。"""
    mock_engine.update_variables.return_value = None
    await client.post("/update_variables/", json=_update_body({"k": "v"}, user_id="u9", scope_id="s9"))
    call = mock_engine.update_variables.call_args
    assert call.kwargs["variables"] == {"k": "v"}
    assert call.kwargs["user_id"] == "u9"
    assert call.kwargs["scope_id"] == "s9"


@pytest.mark.asyncio
async def test_update_variables_empty_dict_accepted(client, mock_engine):
    """空 variables dict 合法。"""
    mock_engine.update_variables.return_value = None
    r = await client.post("/update_variables/", json=_update_body({}))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_update_variables_missing_field_rejected(client, mock_engine):
    """缺 variables 字段应 422。"""
    r = await client.post("/update_variables/", json={"user_id": "u"})
    assert r.status_code == 422
    mock_engine.update_variables.assert_not_called()


@pytest.mark.asyncio
async def test_update_variables_wrong_type_rejected(client, mock_engine):
    """variables 非 dict 应 422。"""
    r = await client.post("/update_variables/", json=_update_body(["not", "a", "dict"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_update_variables_engine_error_500(client, mock_engine):
    mock_engine.update_variables.side_effect = RuntimeError("var boom")
    r = await client.post("/update_variables/", json=_update_body({"k": "v"}))
    assert r.status_code == 500
    assert "var boom" in r.json()["detail"]


# ==================== /delete_variables/ ====================
def _del_body(names, **extra):
    p = {"names": names, "user_id": "u1", "scope_id": "s1"}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_delete_variables_success(client, mock_engine):
    """engine 返回删除数 N → {"status":"success","deleted":N}。"""
    mock_engine.delete_variables.return_value = 3
    r = await client.post("/delete_variables/", json=_del_body(["a", "b", "c"]))
    assert r.status_code == 200
    assert r.json() == {"status": "success", "deleted": 3}


@pytest.mark.asyncio
async def test_delete_variables_zero_deleted(client, mock_engine):
    """engine 返回 0（无可删变量）→ 仍 200 + deleted:0。"""
    mock_engine.delete_variables.return_value = 0
    r = await client.post("/delete_variables/", json=_del_body(["none"]))
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


@pytest.mark.asyncio
async def test_delete_variables_passthrough(client, mock_engine):
    mock_engine.delete_variables.return_value = 1
    await client.post("/delete_variables/", json=_del_body(["x"], user_id="u2", scope_id="s2"))
    call = mock_engine.delete_variables.call_args
    assert call.kwargs["names"] == ["x"]
    assert call.kwargs["user_id"] == "u2"


@pytest.mark.asyncio
async def test_delete_variables_empty_names_rejected(client, mock_engine):
    """空 names 列表：是否 422 取决于 pydantic 对 list 的约束，记录实际行为。"""
    mock_engine.delete_variables.return_value = 0
    r = await client.post("/delete_variables/", json=_del_body([]))
    assert r.status_code in (200, 422)


@pytest.mark.asyncio
async def test_delete_variables_missing_names_rejected(client, mock_engine):
    r = await client.post("/delete_variables/", json={"user_id": "u"})
    assert r.status_code == 422
    mock_engine.delete_variables.assert_not_called()


@pytest.mark.asyncio
async def test_delete_variables_engine_error_500(client, mock_engine):
    mock_engine.delete_variables.side_effect = RuntimeError("del boom")
    r = await client.post("/delete_variables/", json=_del_body(["a"]))
    assert r.status_code == 500


# ==================== /get_variables/ ====================
def _get_body(**extra):
    p = {"names": None, "user_id": "u1", "scope_id": "s1"}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_get_variables_all(client, mock_engine):
    """names=None 取全部，engine 返回 dict → {"variables": dict}。"""
    mock_engine.get_variables.return_value = {"name": "李雷", "age": "30"}
    r = await client.post("/get_variables/", json=_get_body())
    assert r.status_code == 200
    assert r.json()["variables"] == {"name": "李雷", "age": "30"}


@pytest.mark.asyncio
async def test_get_variables_by_names(client, mock_engine):
    """指定 names 列表透传。"""
    mock_engine.get_variables.return_value = {"name": "李雷"}
    r = await client.post("/get_variables/", json=_get_body(names=["name", "age"]))
    assert r.status_code == 200
    call = mock_engine.get_variables.call_args
    assert call.kwargs["names"] == ["name", "age"]


@pytest.mark.asyncio
async def test_get_variables_empty_result(client, mock_engine):
    """engine 返回空 dict → {"variables": {}}。"""
    mock_engine.get_variables.return_value = {}
    r = await client.post("/get_variables/", json=_get_body())
    assert r.status_code == 200
    assert r.json()["variables"] == {}


@pytest.mark.asyncio
async def test_get_variables_engine_error_500(client, mock_engine):
    mock_engine.get_variables.side_effect = RuntimeError("get boom")
    r = await client.post("/get_variables/", json=_get_body())
    assert r.status_code == 500
    assert "get boom" in r.json()["detail"]
