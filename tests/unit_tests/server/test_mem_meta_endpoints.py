# coding: utf-8
"""UT —— /admin/mem_meta/ 端点（refresh / expired_memorys / batch_delete / task_status）。

被测对象：请求模型校验（Pydantic extra=forbid）、参数透传、202/200/404 响应映射、
422 拦截（缺参/非法字段）。Manager 方法全 AsyncMock，隔离真实 DB / Milvus / KV。

Manager 的内部逻辑（SQLAlchemy 异步操作、后台任务、冷却检查）由各自的 UT 覆盖，
本文件只覆盖端点层。
"""
import pytest

from jiuwen_memory.server import mem_meta_api


@pytest.fixture
def mock_manager(mocker):
    """mock 掉 mem_meta_api 模块级 _manager，返回 AsyncMock 实例。

    直接 include router 到 app（不创建真实 MemMetaManager，避免 _init_db 访问 mock db_store）。
    各用例按需对方法 .return_value / .side_effect 设定。
    """
    from jiuwen_memory.server.memory_server import app
    existing_paths = {getattr(r, "path", "") for r in app.routes}
    if not any("/admin/mem_meta" in p for p in existing_paths):
        app.include_router(mem_meta_api.router)
    mgr = mocker.AsyncMock()
    mocker.patch.object(mem_meta_api, "_manager", mgr)
    return mgr


# ==================== 1. refresh ====================

@pytest.mark.asyncio
async def test_refresh_returns_202(client, mock_manager):
    """正常刷新 → 202 + task_id。"""
    mock_manager.submit_refresh.return_value = {
        "status": "accepted",
        "task_id": "tid-001",
        "task_type": "refresh_meta",
        "message": "元数据刷新任务已提交",
    }
    r = await client.post("/admin/mem_meta/refresh", json={"force": False})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["task_id"] == "tid-001"
    mock_manager.submit_refresh.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_refresh_force_true(client, mock_manager):
    """force=true 透传到 manager。"""
    mock_manager.submit_refresh.return_value = {
        "status": "accepted", "task_id": "tid-002",
        "task_type": "refresh_meta", "message": "ok",
    }
    await client.post("/admin/mem_meta/refresh", json={"force": True})
    mock_manager.submit_refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_refresh_skipped_returns_200(client, mock_manager):
    """冷却期/运行中 → 200 + skipped。"""
    mock_manager.submit_refresh.return_value = {
        "status": "skipped", "task_type": "refresh_meta",
        "task_id": "tid-003", "task_status": "running",
        "message": "存在正在执行的任务",
    }
    r = await client.post("/admin/mem_meta/refresh", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


@pytest.mark.asyncio
async def test_refresh_empty_body(client, mock_manager):
    """空 body → 使用默认 force=False。"""
    mock_manager.submit_refresh.return_value = {
        "status": "accepted", "task_id": "t", "task_type": "refresh_meta", "message": "ok",
    }
    await client.post("/admin/mem_meta/refresh", json={})
    mock_manager.submit_refresh.assert_awaited_once_with(force=False)


# ==================== 2. expired_memorys ====================

@pytest.mark.asyncio
async def test_expired_memorys_returns_top_users(client, mock_manager):
    """默认参数查询 → 200 + top_users list。"""
    mock_manager.get_expired_memorys.return_value = {
        "status": "success", "task_id": "t1", "task_status": "completed",
        "inactive_days_threshold": 30, "total_users": 2,
        "total_expired_30d": 100, "users_with_expired": 2,
        "top_users": [
            {"scope_user": "user_001", "total_count": 50, "expired_30d_count": 30},
            {"scope_user": "user_002", "total_count": 80, "expired_30d_count": 70},
        ],
    }
    r = await client.post("/admin/mem_meta/expired_memorys", json={})
    assert r.status_code == 200
    body = r.json()
    assert len(body["top_users"]) == 2
    mock_manager.get_expired_memorys.assert_awaited_once_with(
        inactive_days_threshold=30, limit=10, min_expired_count=0)


@pytest.mark.asyncio
async def test_expired_memorys_custom_params(client, mock_manager):
    """自定义参数透传到 manager。"""
    mock_manager.get_expired_memorys.return_value = {
        "status": "success", "task_id": "t", "task_status": "completed",
        "inactive_days_threshold": 60, "total_users": 0,
        "total_expired_30d": 0, "users_with_expired": 0, "top_users": [],
    }
    await client.post("/admin/mem_meta/expired_memorys", json={
        "inactive_days_threshold": 60, "limit": 5, "min_expired_count": 3,
    })
    mock_manager.get_expired_memorys.assert_awaited_once_with(
        inactive_days_threshold=60, limit=5, min_expired_count=3)


@pytest.mark.asyncio
async def test_expired_memorys_scanning_status(client, mock_manager):
    """refresh 在跑时返回 scanning 状态。"""
    mock_manager.get_expired_memorys.return_value = {
        "status": "scanning", "task_id": "t", "task_status": "running",
        "inactive_days_threshold": 30, "total_users": 0,
        "total_expired_30d": 0, "users_with_expired": 0, "top_users": [],
    }
    r = await client.post("/admin/mem_meta/expired_memorys", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "scanning"


@pytest.mark.asyncio
async def test_expired_memorys_limit_422(client, mock_manager):
    """limit > 100 → 422（Pydantic Field le=100）。"""
    r = await client.post("/admin/mem_meta/expired_memorys", json={"limit": 200})
    assert r.status_code == 422
    mock_manager.get_expired_memorys.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_memorys_threshold_422(client, mock_manager):
    """inactive_days_threshold=0 → 422（Field ge=1）。"""
    r = await client.post("/admin/mem_meta/expired_memorys", json={"inactive_days_threshold": 0})
    assert r.status_code == 422


# ==================== 3. batch_delete ====================

@pytest.mark.asyncio
async def test_batch_delete_returns_202(client, mock_manager):
    """正常删除 → 202 + task_id。"""
    mock_manager.submit_batch_delete.return_value = {
        "status": "accepted", "task_id": "tid-del-1",
        "task_type": "batch_delete",
        "message": "批量删除任务已提交，涉及 1 个用户", "total_users": 1,
    }
    r = await client.post("/admin/mem_meta/batch_delete", json={
        "user_ids": ["user_001"], "dry_run": False, "force": True,
    })
    assert r.status_code == 202
    assert r.json()["task_id"] == "tid-del-1"


@pytest.mark.asyncio
async def test_batch_delete_dry_run(client, mock_manager):
    """dry_run=true 透传到 manager。"""
    mock_manager.submit_batch_delete.return_value = {
        "status": "accepted", "task_id": "t", "task_type": "batch_delete",
        "message": "ok", "total_users": 1,
    }
    await client.post("/admin/mem_meta/batch_delete", json={
        "user_ids": ["user_001"], "dry_run": True, "force": True,
    })
    args = mock_manager.submit_batch_delete.call_args
    params = args.args[0]
    assert params.dry_run is True
    assert params.user_ids == ["user_001"]


@pytest.mark.asyncio
async def test_batch_delete_all_expired(client, mock_manager):
    """all_expired=true 透传到 manager。"""
    mock_manager.submit_batch_delete.return_value = {
        "status": "accepted", "task_id": "t", "task_type": "batch_delete",
        "message": "ok", "total_users": 5,
    }
    await client.post("/admin/mem_meta/batch_delete", json={
        "all_expired": True, "force": True,
    })
    args = mock_manager.submit_batch_delete.call_args
    params = args.args[0]
    assert params.all_expired is True
    assert params.user_ids is None


@pytest.mark.asyncio
async def test_batch_delete_skipped_returns_200(client, mock_manager):
    """冷却期/运行中 → 200 + skipped。"""
    mock_manager.submit_batch_delete.return_value = {
        "status": "skipped", "task_type": "batch_delete",
        "task_id": "tid", "task_status": "completed",
        "message": "冷却期内（<300秒）",
    }
    r = await client.post("/admin/mem_meta/batch_delete", json={
        "user_ids": ["user_001"],
    })
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


@pytest.mark.asyncio
async def test_batch_delete_missing_params_422(client, mock_manager):
    """缺 user_ids 且 all_expired=false → 422。"""
    r = await client.post("/admin/mem_meta/batch_delete", json={})
    assert r.status_code == 422
    mock_manager.submit_batch_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_delete_invalid_field_422(client, mock_manager):
    """非法字段 → 422（Pydantic extra=forbid）。"""
    r = await client.post("/admin/mem_meta/batch_delete", json={
        "user_ids": ["u1"], "bad_field": "x",
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_batch_delete_scope_id_passthrough(client, mock_manager):
    """scope_id 透传到 manager。"""
    mock_manager.submit_batch_delete.return_value = {
        "status": "accepted", "task_id": "t", "task_type": "batch_delete",
        "message": "ok", "total_users": 1,
    }
    await client.post("/admin/mem_meta/batch_delete", json={
        "user_ids": ["user_001"], "scope_id": "scope_a", "force": True,
    })
    args = mock_manager.submit_batch_delete.call_args
    params = args.args[0]
    assert params.scope_id == "scope_a"


# ==================== 4. task_status ====================

@pytest.mark.asyncio
async def test_task_status_by_id(client, mock_manager):
    """按 task_id 查询 → 200 + task 详情。"""
    mock_manager.get_task_status.return_value = {
        "status": "success",
        "task": {
            "task_id": "tid-xyz", "task_type": "batch_delete",
            "status": "completed", "deleted_count": 42,
        },
    }
    r = await client.get("/admin/mem_meta/task_status?task_id=tid-xyz")
    assert r.status_code == 200
    assert r.json()["task"]["task_id"] == "tid-xyz"
    assert r.json()["task"]["deleted_count"] == 42


@pytest.mark.asyncio
async def test_task_status_latest(client, mock_manager):
    """无参数查询最新 → 200 + task 非空。"""
    mock_manager.get_task_status.return_value = {
        "status": "success",
        "task": {
            "task_id": "tid-latest", "task_type": "refresh_meta",
            "status": "completed",
        },
    }
    r = await client.get("/admin/mem_meta/task_status")
    assert r.status_code == 200
    assert r.json()["task"]["task_id"] == "tid-latest"
    mock_manager.get_task_status.assert_awaited_once_with(task_id=None)


@pytest.mark.asyncio
async def test_task_status_not_found_404(client, mock_manager):
    """不存在的 task_id → 404。"""
    mock_manager.get_task_status.return_value = {
        "status": "not_found",
        "task_id": "nonexistent",
        "message": "任务 nonexistent 不存在",
    }
    r = await client.get("/admin/mem_meta/task_status?task_id=nonexistent")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


@pytest.mark.asyncio
async def test_task_status_null_task(client, mock_manager):
    """空表无任务 → 200 + task=null。"""
    mock_manager.get_task_status.return_value = {
        "status": "success", "task": None, "message": "无任务记录",
    }
    r = await client.get("/admin/mem_meta/task_status")
    assert r.status_code == 200
    assert r.json()["task"] is None
