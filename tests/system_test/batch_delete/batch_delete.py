# coding: utf-8
import asyncio
import json

import pytest


class TestRefreshMeta:
    """接口1 POST /admin/mem_meta/refresh 集成测试"""

    @pytest.mark.asyncio
    async def test_refresh_normal_returns_202(self, reset_client):
        """TC-01: 正常刷新 → 202 + task_id"""
        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "accepted"
        assert "task_id" in data
        assert data["task_type"] == "refresh_meta"

    @pytest.mark.asyncio
    async def test_refresh_scans_and_completes(self, reset_client):
        """TC-02: 后台扫描完成后 task_status=completed"""
        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)  # 等待后台扫描完成

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        assert r.status_code == 200
        task = r.json()["task"]
        assert task["status"] == "completed"
        assert task["result_summary"] is not None
        summary = json.loads(task["result_summary"])
        assert summary["total_scanned"] > 0
        assert summary["total_users"] > 0

    @pytest.mark.asyncio
    async def test_cooldown_returns_skipped(self, reset_client):
        """TC-03: 冷却期内再次刷新 → 200 + skipped"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.2)

        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"
        assert "冷却" in r.json()["message"]

    @pytest.mark.asyncio
    async def test_force_skips_cooldown(self, reset_client):
        """TC-04: force=true 强制刷新 → 202"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.2)

        r = await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
        assert r.json()["task_id"] is not None

    @pytest.mark.asyncio
    async def test_invalid_field_returns_422(self, reset_client):
        """TC-05: 非法字段 → 422 (Pydantic extra=forbid)"""
        r = await reset_client.post(
            "/admin/mem_meta/refresh",
            json={"force": True, "invalid_field": "x"})
        assert r.status_code == 422


class TestExpiredMemorys:
    """接口2 POST /admin/mem_meta/expired_memorys 集成测试"""

    @pytest.mark.asyncio
    async def test_default_top10(self, reset_client):
        """TC-06: 默认参数查询 → 200 + top_users list"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post("/admin/mem_meta/expired_memorys", json={})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["top_users"], list)
        assert data["total_users"] > 0
        assert data["inactive_days_threshold"] == 30

    @pytest.mark.asyncio
    async def test_limit_5(self, reset_client):
        """TC-07: limit=5 → top_users.length <= 5"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys", json={"limit": 5})
        assert r.status_code == 200
        assert len(r.json()["top_users"]) <= 5

    @pytest.mark.asyncio
    async def test_limit_1(self, reset_client):
        """TC-08: limit=1 → 最多返回 1 个用户"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys", json={"limit": 1})
        assert r.status_code == 200
        assert len(r.json()["top_users"]) <= 1

    @pytest.mark.asyncio
    async def test_min_expired_filter(self, reset_client):
        """TC-09: min_expired_count=1 → 返回用户 expired_30d_count >= 1"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys", json={"min_expired_count": 1})
        assert r.status_code == 200
        for u in r.json()["top_users"]:
            assert u["expired_30d_count"] >= 1

    @pytest.mark.asyncio
    async def test_limit_200_returns_422(self, reset_client):
        """TC-10: limit=200 超限 → 422"""
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys", json={"limit": 200})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_realtime_scan_threshold_not_30(self, reset_client):
        """TC-11: inactive_days_threshold=1 走实时扫描路径（仅返回 expired>0 的用户）"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        assert r.status_code == 200
        data = r.json()
        assert data["inactive_days_threshold"] == 1
        for u in data["top_users"]:
            assert u["expired_30d_count"] > 0

    @pytest.mark.asyncio
    async def test_empty_table_query(self, mock_db):
        """TC-12: 空表查询 → total_users=0, top_users=[]"""
        from tests.system_test.conftest import _build_app, _init_db
        # 用空 DB（只建表不填充数据）
        import sqlite3
        import os
        empty_path = mock_db.replace(".db", "_empty.db")
        conn = sqlite3.connect(empty_path)
        conn.close()
        _init_db(empty_path)
        # 清空 av_user_stats
        conn = sqlite3.connect(empty_path)
        conn.execute("DELETE FROM av_user_stats")
        conn.commit()
        conn.close()

        app = _build_app(empty_path)
        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/admin/mem_meta/expired_memorys", json={})
            assert r.status_code == 200
            data = r.json()
            assert data["total_users"] == 0
            assert data["top_users"] == []

        if os.path.exists(empty_path):
            os.remove(empty_path)


class TestBatchDelete:
    """接口3 POST /admin/mem_meta/batch_delete 集成测试"""

    @pytest.mark.asyncio
    async def test_dry_run_preview(self, reset_client):
        """TC-13: dry_run=true 预览 → 202 + 不实际删除"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": True, "force": True})
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        summary = json.loads(task["result_summary"])
        assert summary["dry_run"] is True
        assert summary["deleted"] > 0

    @pytest.mark.asyncio
    async def test_missing_params_returns_422(self, reset_client):
        """TC-14: 缺少 user_ids 和 all_expired → 422"""
        r = await reset_client.post("/admin/mem_meta/batch_delete", json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_all_expired_async_delete(self, reset_client):
        """TC-15: all_expired=true → 202 + 异步删除"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"all_expired": True, "force": True})
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        assert task["deleted_count"] > 0

    @pytest.mark.asyncio
    async def test_invalid_field_returns_422(self, reset_client):
        """TC-16: 非法字段 → 422"""
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"all_expired": True, "bad": 1})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_real_delete_decreases_expired(self, reset_client):
        """TC-17: 正式删除后过期记忆数减少"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        # 删除前查询
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        before_count = r.json()["total_users"]

        # 正式删除 test_e2e_user
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": False, "force": True})
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        assert r.json()["task"]["status"] == "completed"
        assert r.json()["task"]["deleted_count"] > 0

        # 删除后查询
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        after_count = r.json()["total_users"]
        assert after_count < before_count

    @pytest.mark.asyncio
    async def test_cooldown_skips_delete(self, reset_client):
        """TC-18: 冷却期内再次删除 → 200 + skipped"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "force": True})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"]})
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_user_not_in_stats_skipped(self, reset_client):
        """TC-19: 不在 av_user_stats 中的用户 → skipped"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["unknown_user"], "force": True})
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        task = r.json()["task"]
        summary = json.loads(task["result_summary"])
        assert summary["failed"] == 1
        assert task["status"] == "failed"


class TestTaskStatus:
    """接口4 GET /admin/mem_meta/task_status 集成测试"""

    @pytest.mark.asyncio
    async def test_query_by_task_id(self, reset_client):
        """TC-20: 按 task_id 查询 → 200 + task_id 匹配"""
        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        task_id = r.json()["task_id"]

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "success"
        assert r.json()["task"]["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_query_latest_no_param(self, reset_client):
        """TC-21: 无参数查询最新 → 200 + task 非空"""
        await reset_client.post("/admin/mem_meta/refresh", json={})

        r = await reset_client.get("/admin/mem_meta/task_status")
        assert r.status_code == 200
        assert r.json()["task"] is not None
        assert r.json()["task"]["task_type"] == "refresh_meta"

    @pytest.mark.asyncio
    async def test_nonexistent_returns_404(self, reset_client):
        """TC-22: 不存在的 task_id → 404"""
        r = await reset_client.get(
            "/admin/mem_meta/task_status?task_id=nonexistent-xxx")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_task_has_complete_fields(self, reset_client):
        """TC-23: 任务对象包含完整字段"""
        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(f"/admin/mem_meta/task_status?task_id={task_id}")
        task = r.json()["task"]
        for field in ["task_id", "task_type", "status", "created_at",
                      "updated_at", "started_at", "finished_at"]:
            assert field in task, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_empty_db_returns_null_task(self, mock_db):
        """TC-24: 空表无任务时 → task=null"""
        from tests.system_test.conftest import _build_app
        app = _build_app(mock_db)
        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/admin/mem_meta/task_status")
            assert r.status_code == 200
            assert r.json()["task"] is None


class TestDataIntegrity:
    """数据一致性验证"""

    @pytest.mark.asyncio
    async def test_total_equals_active_plus_superseded(self, reset_client):
        """TC-25: av_user_stats 中 total = active + superseded"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"limit": 100})
        for u in r.json()["top_users"]:
            assert u["total_count"] == u["active_count"] + u["superseded_count"]

    @pytest.mark.asyncio
    async def test_delete_updates_av_user_stats(self, reset_client):
        """TC-26: 删除后 av_user_stats 同步更新（expired_30d=0）"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": False, "force": True})
        await asyncio.sleep(0.3)

        # 验证 test_e2e_user 的 expired_30d_count 变为 0
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        users = r.json()["top_users"]
        e2e = [u for u in users if u["scope_user"] == "test_e2e_user"]
        assert len(e2e) == 0, "test_e2e_user 应不在过期列表中"

    @pytest.mark.asyncio
    async def test_dry_run_preserves_data(self, reset_client):
        """TC-27: dry_run 不实际删除数据"""
        await reset_client.post("/admin/mem_meta/refresh", json={})
        await asyncio.sleep(0.3)

        # dry_run 前查询
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        before = r.json()["total_users"]

        # dry_run
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": True, "force": True})
        await asyncio.sleep(0.3)

        # dry_run 后查询
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        after = r.json()["total_users"]
        assert after == before, "dry_run 不应改变数据"


class TestFullChainSequence:
    """全链路串联测试

    验证: refresh → task_status → expired_memorys → batch_delete(dry_run)
         → batch_delete(real) → task_status → 验证删除
    """

    @pytest.mark.asyncio
    async def test_full_chain_refresh_and_verify(self, reset_client):
        """TC-28: refresh → task_status 验证完成"""
        # Step 1: refresh
        r = await reset_client.post(
            "/admin/mem_meta/refresh", json={"force": True})
        assert r.status_code == 202
        refresh_tid = r.json()["task_id"]

        # Step 2: 等待完成
        await asyncio.sleep(0.3)
        r = await reset_client.get(
            f"/admin/mem_meta/task_status?task_id={refresh_tid}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        summary = json.loads(task["result_summary"])
        assert summary["total_scanned"] > 0

    @pytest.mark.asyncio
    async def test_full_chain_expired_and_dry_run(self, reset_client):
        """TC-29: expired_memorys → batch_delete(dry_run) → 验证预览"""
        # 前置: refresh
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(0.3)

        # Step 3: expired_memorys (实时扫描)
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        assert r.status_code == 200
        before_count = len(r.json()["top_users"])
        assert before_count > 0

        # Step 4: dry_run 预览
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": True, "force": True})
        dry_tid = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(
            f"/admin/mem_meta/task_status?task_id={dry_tid}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        summary = json.loads(task["result_summary"])
        assert summary["dry_run"] is True
        assert summary["deleted"] > 0

    @pytest.mark.asyncio
    async def test_full_chain_real_delete_and_verify(self, reset_client):
        """TC-30: batch_delete(real) → task_status → 验证删除结果"""
        # 前置: refresh
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(0.3)

        # Step 5: 正式删除
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": False, "force": True})
        del_tid = r.json()["task_id"]
        await asyncio.sleep(0.3)

        # Step 6: 查询删除任务状态
        r = await reset_client.get(
            f"/admin/mem_meta/task_status?task_id={del_tid}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        assert task["deleted_count"] > 0
        assert task["failed_count"] == 0

        # Step 7: 验证删除结果
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        users = r.json()["top_users"]
        found = [u for u in users if u["scope_user"] == "test_e2e_user"]
        assert len(found) == 0, "test_e2e_user 不应出现在过期列表中"

    @pytest.mark.asyncio
    async def test_full_chain_all_expired_batch(self, reset_client):
        """TC-31: all_expired=true 删除全部过期用户 → 验证全部清除"""
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(0.3)

        # 查询删除前过期用户数
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        before = r.json()["total_users"]
        assert before > 0

        # all_expired 删除
        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"all_expired": True, "force": True})
        task_id = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get(
            f"/admin/mem_meta/task_status?task_id={task_id}")
        task = r.json()["task"]
        assert task["status"] == "completed"
        assert task["deleted_count"] > 0

        # 删除后过期用户数应为 0
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        after = r.json()["total_users"]
        assert after == 0

    @pytest.mark.asyncio
    async def test_full_chain_latest_task_after_sequence(self, reset_client):
        """TC-32: 全链路后查最新任务 → 应为最后执行的 batch_delete"""
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(1.1)  # 确保秒级时间戳变化

        r = await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"user_ids": ["test_e2e_user"], "dry_run": True, "force": True})
        batch_tid = r.json()["task_id"]
        await asyncio.sleep(0.3)

        r = await reset_client.get("/admin/mem_meta/task_status")
        task = r.json()["task"]
        assert task["task_id"] == batch_tid
        assert task["task_type"] == "batch_delete"
        assert task["status"] == "completed"

    @pytest.mark.asyncio
    async def test_full_chain_reset_between_tests(self, reset_client):
        """TC-33: reset 接口可重置数据 → 可重复测试"""
        # 执行一些操作
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(0.3)
        await reset_client.post(
            "/admin/mem_meta/batch_delete",
            json={"all_expired": True, "force": True})
        await asyncio.sleep(0.3)

        # reset
        r = await reset_client.post("/admin/mem_meta/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "success"

        # 验证数据恢复
        await reset_client.post("/admin/mem_meta/refresh", json={"force": True})
        await asyncio.sleep(0.3)
        r = await reset_client.post(
            "/admin/mem_meta/expired_memorys",
            json={"inactive_days_threshold": 1, "limit": 100})
        assert r.json()["total_users"] > 0, "reset 后应恢复过期用户数据"

    @pytest.mark.asyncio
    async def test_full_chain_force_then_cooldown_then_force(self, reset_client):
        """TC-34: force刷新 → 冷却拦截 → force再刷新 → 验证状态流转"""
        # 1. force 刷新
        r = await reset_client.post(
            "/admin/mem_meta/refresh", json={"force": True})
        assert r.status_code == 202
        tid1 = r.json()["task_id"]
        await asyncio.sleep(0.3)

        # 2. 冷却期内再刷新 → skipped
        r = await reset_client.post("/admin/mem_meta/refresh", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "skipped"
        assert r.json()["task_id"] == tid1

        # 3. force 跳过冷却
        r = await reset_client.post(
            "/admin/mem_meta/refresh", json={"force": True})
        assert r.status_code == 202
        tid2 = r.json()["task_id"]
        assert tid2 != tid1
        await asyncio.sleep(0.3)

        # 4. 验证两个任务都完成
        for tid in [tid1, tid2]:
            r = await reset_client.get(
                f"/admin/mem_meta/task_status?task_id={tid}")
            assert r.json()["task"]["status"] == "completed"
