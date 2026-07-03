# coding: utf-8
"""server UT —— 记忆变更端点 /update_mem_by_id/ /delete_mem_by_scope/。

被测对象：mem_id/memory/scope_id 透传、engine 返回值序列化、异常转 500、422 校验。
"""
import pytest


# ==================== /update_mem_by_id/ ====================
def _update_mem_body(mem_id="m1", memory="new content", **extra):
    p = {"mem_id": mem_id, "memory": memory, "user_id": "u1", "scope_id": "s1"}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_update_mem_success(client, mock_engine):
    mock_engine.update_mem_by_id.return_value = None
    r = await client.post("/update_mem_by_id/", json=_update_mem_body())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert "m1" in body["message"]


@pytest.mark.asyncio
async def test_update_mem_passthrough(client, mock_engine):
    mock_engine.update_mem_by_id.return_value = None
    await client.post("/update_mem_by_id/", json=_update_mem_body(
        mem_id="m99", memory="更新后的内容", user_id="u9", scope_id="s9"))
    call = mock_engine.update_mem_by_id.call_args
    assert call.kwargs["mem_id"] == "m99"
    assert call.kwargs["memory"] == "更新后的内容"
    assert call.kwargs["user_id"] == "u9"
    assert call.kwargs["scope_id"] == "s9"


@pytest.mark.asyncio
async def test_update_mem_missing_mem_id_rejected(client, mock_engine):
    r = await client.post("/update_mem_by_id/", json={"memory": "x"})
    assert r.status_code == 422
    mock_engine.update_mem_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_update_mem_missing_memory_rejected(client, mock_engine):
    r = await client.post("/update_mem_by_id/", json={"mem_id": "m1"})
    assert r.status_code == 422
    mock_engine.update_mem_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_update_mem_empty_memory_accepted(client, mock_engine):
    """空串 memory 是合法 str，通过校验（是否允许空内容由 engine 决定）。"""
    mock_engine.update_mem_by_id.return_value = None
    r = await client.post("/update_mem_by_id/", json=_update_mem_body(memory=""))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_update_mem_engine_error_500(client, mock_engine):
    mock_engine.update_mem_by_id.side_effect = RuntimeError("update boom")
    r = await client.post("/update_mem_by_id/", json=_update_mem_body())
    assert r.status_code == 500
    assert "update boom" in r.json()["detail"]


@pytest.mark.asyncio
async def test_update_mem_not_found_error_500(client, mock_engine):
    """engine 因 mem_id 不存在抛异常 → 端点 500（不伪装成 404）。"""
    mock_engine.update_mem_by_id.side_effect = ValueError("mem not found")
    r = await client.post("/update_mem_by_id/", json=_update_mem_body(mem_id="ghost"))
    assert r.status_code == 500


# ==================== /delete_mem_by_scope/ ====================
def _del_scope_body(scope_id="s1"):
    return {"scope_id": scope_id}


@pytest.mark.asyncio
async def test_delete_by_scope_success(client, mock_engine):
    """engine 返回删除数 → {"status":"success","deleted":N}。"""
    mock_engine.delete_mem_by_scope.return_value = 5
    r = await client.post("/delete_mem_by_scope/", json=_del_scope_body("s_to_clear"))
    assert r.status_code == 200
    assert r.json() == {"status": "success", "deleted": 5}


@pytest.mark.asyncio
async def test_delete_by_scope_zero(client, mock_engine):
    mock_engine.delete_mem_by_scope.return_value = 0
    r = await client.post("/delete_mem_by_scope/", json=_del_scope_body("empty_scope"))
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


@pytest.mark.asyncio
async def test_delete_by_scope_passthrough(client, mock_engine):
    """注意：delete_mem_by_scope 只透传 scope_id，无 user_id。"""
    mock_engine.delete_mem_by_scope.return_value = 1
    await client.post("/delete_mem_by_scope/", json=_del_scope_body("sc_xyz"))
    call = mock_engine.delete_mem_by_scope.call_args
    assert call.kwargs["scope_id"] == "sc_xyz"


@pytest.mark.asyncio
async def test_delete_by_scope_missing_field_rejected(client, mock_engine):
    r = await client.post("/delete_mem_by_scope/", json={})
    assert r.status_code == 422
    mock_engine.delete_mem_by_scope.assert_not_called()


@pytest.mark.asyncio
async def test_delete_by_scope_engine_error_500(client, mock_engine):
    mock_engine.delete_mem_by_scope.side_effect = RuntimeError("scope del boom")
    r = await client.post("/delete_mem_by_scope/", json=_del_scope_body())
    assert r.status_code == 500
    assert "scope del boom" in r.json()["detail"]
