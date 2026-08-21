# coding: utf-8
import os
import json
import time
import uuid
import sqlite3
import threading
from datetime import datetime
from typing import Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# 测试配置（不读 .env，全部在此定义）
# ============================================================

COOLDOWN_SECONDS = 2     # 冷却秒数（生产为 300，测试缩短）
SCAN_DELAY = 0.1          # refresh 扫描模拟延迟（秒）
DELETE_DELAY = 0.1        # batch_delete 删除模拟延迟（秒）

AV_COLS = [
    "scope_user", "total_count", "active_count", "superseded_count",
    "expired_30d_count", "expired_all_count",
    "tier_episodic", "tier_semantic", "tier_other",
    "created_at", "updated_at",
]

TASK_COLS = [
    "task_id", "task_type", "status", "request_params", "result_summary",
    "error_message", "total_users", "processed_users", "deleted_count",
    "failed_count", "created_at", "updated_at", "started_at", "finished_at",
]

# 预填充测试数据
TEST_USERS = [
    ("tc_user_056",     1281, 1220, 61,  61,  61,  1281, 0, 0),
    ("tc_user_078",      500,  450, 50,  50,  50,   500, 0, 0),
    ("test_user_001",    100,   80, 20,  20,  20,   100, 0, 0),
    ("test_e2e_user",     33,    0, 33,  33,  33,    33, 0, 0),
    ("test_user_002",     50,   50,  0,   0,   0,    50, 0, 0),
    ("test_user_010",   1250, 1100, 150, 150, 150,  1250, 0, 0),
    ("no_expired_user",   30,   30,  0,   0,   0,    30, 0, 0),
]

# ============================================================
# Pydantic 请求模型（与源码 mem_meta_api.py 一致）
# ============================================================


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class ExpiredMemorysRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inactive_days_threshold: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=100)
    min_expired_count: int = Field(default=0, ge=0)


class BatchDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: Optional[list[str]] = None
    all_expired: bool = False
    inactive_days_threshold: int = Field(default=30, ge=1, le=365)
    scope_id: Optional[str] = None
    cleanup_retrieve_history: bool = True
    dry_run: bool = False
    force: bool = False


# ============================================================
# 辅助函数
# ============================================================

def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _init_db(db_path: str) -> None:
    """初始化 SQLite + 预填充测试数据"""
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE av_user_stats (
            scope_user TEXT PRIMARY KEY, total_count INTEGER NOT NULL,
            active_count INTEGER NOT NULL, superseded_count INTEGER NOT NULL,
            expired_30d_count INTEGER NOT NULL, expired_all_count INTEGER NOT NULL,
            tier_episodic INTEGER DEFAULT 0, tier_semantic INTEGER DEFAULT 0,
            tier_other INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE mem_meta_task (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', request_params TEXT,
            result_summary TEXT, error_message TEXT,
            total_users INTEGER DEFAULT 0, processed_users INTEGER DEFAULT 0,
            deleted_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON mem_meta_task(status)")
    now = _now_str()
    for u in TEST_USERS:
        cur.execute(
            "INSERT INTO av_user_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7], u[8], now, now))
    conn.commit()
    conn.close()


def _build_app(db_path: str) -> FastAPI:
    """构建带 mem_meta 端点的 FastAPI app（自包含，不依赖项目源码）"""

    def db_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def check_guard(task_type: str, force: bool = False):
        if force:
            return None
        conn = db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT task_id, status FROM mem_meta_task "
                "WHERE task_type=? AND status='running' ORDER BY created_at DESC LIMIT 1",
                (task_type,))
            row = cur.fetchone()
            if row:
                return {"task_id": row[0], "status": row[1], "message": "存在正在执行的任务"}
            cur.execute(
                "SELECT task_id, status, created_at FROM mem_meta_task "
                "WHERE task_type=? ORDER BY created_at DESC LIMIT 1", (task_type,))
            row = cur.fetchone()
            if row:
                elapsed = (datetime.utcnow() -
                           datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")).total_seconds()
                if elapsed < COOLDOWN_SECONDS:
                    return {"task_id": row[0], "status": row[1], "message": f"冷却期内（<{COOLDOWN_SECONDS}秒）"}
            return None
        finally:
            conn.close()

    def create_task(task_type, request_params=None, total_users=0):
        task_id = str(uuid.uuid4())
        now = _now_str()
        params_json = json.dumps(request_params, ensure_ascii=False) if request_params else None
        conn = db_conn()
        conn.execute(
            "INSERT INTO mem_meta_task (task_id,task_type,status,request_params,"
            "total_users,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, task_type, "pending", params_json, total_users, now, now))
        conn.commit()
        conn.close()
        return task_id

    def update_task(task_id, **fields):
        conn = db_conn()
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.extend([_now_str(), task_id])
        conn.execute(
                f"UPDATE mem_meta_task SET {','.join(sets)} "
                f"WHERE task_id=?", vals)  # nosec B608
        conn.commit()
        conn.close()

    def _bg_refresh(task_id):
        try:
            time.sleep(0.05)
            update_task(task_id, status="running", started_at=_now_str())
            time.sleep(SCAN_DELAY)
            conn = db_conn()
            cur = conn.cursor()
            total_users = cur.execute("SELECT COUNT(*) FROM av_user_stats").fetchone()[0]
            total_scanned = cur.execute("SELECT COALESCE(SUM(total_count),0) FROM av_user_stats").fetchone()[0]
            expired = cur.execute("SELECT COALESCE(SUM(expired_30d_count),0) FROM av_user_stats").fetchone()[0]
            conn.close()
            update_task(
                task_id, status="completed", finished_at=_now_str(),
                result_summary=json.dumps({
                    "total_scanned": total_scanned,
                    "total_users": total_users,
                    "expired_30d": expired}))
        except Exception:  # nosec B110
            pass  # DB 可能已被 fixture 清理

    def _bg_delete(task_id, user_ids, dry_run):
        try:
            time.sleep(0.05)
            update_task(task_id, status="running", started_at=_now_str())
            time.sleep(DELETE_DELAY)
            conn = db_conn()
            cur = conn.cursor()
            deleted_total, failed, skipped, details = 0, 0, 0, []
            for uid in user_ids:
                cur.execute(
                    "SELECT total_count, active_count, superseded_count, "
                    "expired_30d_count, expired_all_count FROM av_user_stats WHERE scope_user=?", (uid,))
                row = cur.fetchone()
                if not row:
                    details.append({"user_id": uid, "status": "skipped",
                                    "error": "not found in av_user_stats"})
                    failed += 1
                    continue
                if row[3] == 0:
                    details.append({"user_id": uid, "status": "skipped",
                                    "error": "expired_30d_count=0"})
                    skipped += 1
                    continue
                will_delete = row[3]
                if not dry_run:
                    conn.execute(
                        "UPDATE av_user_stats SET total_count=?, superseded_count=?, "
                        "expired_30d_count=?, updated_at=? WHERE scope_user=?",
                        (max(0, row[0]-will_delete), max(0, row[2]-will_delete),
                         0, _now_str(), uid))
                    conn.commit()
                deleted_total += will_delete
                details.append({"user_id": uid, "status": "success",
                                "total_deleted": will_delete, "scopes_affected": 1})
            conn.close()
            final = "completed" if failed == 0 else (
                "failed" if failed == len(user_ids) else "partial_failed")
            update_task(
                task_id, status=final, finished_at=_now_str(),
                processed_users=len(user_ids), deleted_count=deleted_total,
                failed_count=failed,
                result_summary=json.dumps({
                    "processed": len(user_ids),
                    "deleted": deleted_total, "failed": failed,
                    "skipped": skipped, "dry_run": dry_run,
                    "details": details}))
        except Exception:  # nosec B110
            pass  # DB 可能已被 fixture 清理

    app = FastAPI(title="mem_meta System Test")

    @app.post("/admin/mem_meta/refresh")
    async def refresh_meta(req: RefreshRequest):
        existing = check_guard("refresh_meta", force=req.force)
        if existing:
            return {"status": "skipped", "task_type": "refresh_meta",
                    "task_id": existing.get("task_id"),
                    "task_status": existing.get("status"),
                    "message": existing.get("message", "")}
        task_id = create_task("refresh_meta", request_params={"force": req.force})
        threading.Thread(target=_bg_refresh, args=(task_id,), daemon=True).start()
        return Response(
            status_code=202,
            content=json.dumps(
                {"status": "accepted", "task_id": task_id,
                 "task_type": "refresh_meta",
                 "message": "元数据刷新任务已提交"},
                ensure_ascii=False),
            media_type="application/json")

    @app.post("/admin/mem_meta/expired_memorys")
    async def get_expired_memorys(req: ExpiredMemorysRequest):
        conn = db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT task_id,status FROM mem_meta_task "
                "WHERE task_type='refresh_meta' "
                "ORDER BY created_at DESC LIMIT 1")
            t = cur.fetchone()
            task_id, task_status = (t[0], t[1]) if t else (None, "none")

            if req.inactive_days_threshold == 30:
                total_users = cur.execute("SELECT COUNT(*) FROM av_user_stats").fetchone()[0]
                total_exp = cur.execute("SELECT COALESCE(SUM(expired_30d_count),0) FROM av_user_stats").fetchone()[0]
                uw_exp = cur.execute("SELECT COUNT(*) FROM av_user_stats WHERE expired_30d_count>0").fetchone()[0]
                cur.execute(
                    "SELECT scope_user,total_count,active_count,superseded_count,"
                    "expired_30d_count,expired_all_count,tier_episodic,tier_semantic,"
                    "tier_other,created_at,updated_at FROM av_user_stats "
                    "WHERE expired_30d_count>=? ORDER BY expired_30d_count DESC LIMIT ?",
                    (req.min_expired_count, req.limit))
            else:
                total_users = cur.execute(
                    "SELECT COUNT(*) FROM av_user_stats "
                    "WHERE expired_30d_count>0").fetchone()[0]
                total_exp = cur.execute(
                    "SELECT COALESCE(SUM(expired_30d_count),0) "
                    "FROM av_user_stats WHERE expired_30d_count>0").fetchone()[0]
                uw_exp = total_users
                cur.execute(
                    "SELECT scope_user,total_count,active_count,superseded_count,"
                    "expired_30d_count,expired_all_count,tier_episodic,tier_semantic,"
                    "tier_other,created_at,updated_at FROM av_user_stats "
                    "WHERE expired_30d_count>0 AND expired_30d_count>=? "
                    "ORDER BY expired_30d_count DESC LIMIT ?",
                    (req.min_expired_count, req.limit))
            users = [dict(zip(AV_COLS, r)) for r in cur.fetchall()]
            return {"status": "scanning" if task_status == "running" else "success",
                    "task_id": task_id, "task_status": task_status,
                    "inactive_days_threshold": req.inactive_days_threshold,
                    "total_users": total_users, "total_expired_30d": total_exp,
                    "users_with_expired": uw_exp, "top_users": users}
        finally:
            conn.close()

    @app.post("/admin/mem_meta/batch_delete")
    async def batch_delete(req: BatchDeleteRequest):
        if not req.user_ids and not req.all_expired:
            raise HTTPException(status_code=422, detail="必须指定 user_ids 或 all_expired=true")
        if req.all_expired:
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT scope_user FROM av_user_stats WHERE expired_30d_count>0")
            req.user_ids = [r[0] for r in cur.fetchall()]
            conn.close()
        if not req.user_ids:
            return {"status": "success", "message": "无过期用户", "total_users": 0}
        existing = check_guard("batch_delete", force=req.force)
        if existing:
            return {"status": "skipped", "task_type": "batch_delete",
                    "task_id": existing.get("task_id"),
                    "task_status": existing.get("status"),
                    "message": existing.get("message", "")}
        task_id = create_task("batch_delete", request_params={
            "user_ids": req.user_ids, "all_expired": req.all_expired,
            "dry_run": req.dry_run}, total_users=len(req.user_ids))
        threading.Thread(target=_bg_delete, args=(task_id, req.user_ids, req.dry_run), daemon=True).start()
        return Response(
            status_code=202,
            content=json.dumps(
                {"status": "accepted", "task_id": task_id,
                 "task_type": "batch_delete",
                 "message": f"批量删除任务已提交，涉及 {len(req.user_ids)} 个用户",
                 "total_users": len(req.user_ids)},
                ensure_ascii=False),
            media_type="application/json")

    @app.get("/admin/mem_meta/task_status")
    async def get_task_status(task_id: Optional[str] = None):
        conn = db_conn()
        try:
            cur = conn.cursor()
            if task_id:
                cur.execute("SELECT * FROM mem_meta_task WHERE task_id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
            else:
                cur.execute("SELECT * FROM mem_meta_task ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return {"status": "success", "task": None, "message": "无任务记录"}
            return {"status": "success", "task": dict(zip(TASK_COLS, row))}
        finally:
            conn.close()

    @app.post("/admin/mem_meta/reset")
    async def reset_data():
        _init_db(db_path)
        return {"status": "success", "message": "数据已重置", "users": len(TEST_USERS)}

    return app


# ============================================================
# Pytest Fixtures
# ============================================================

@pytest.fixture
def mock_db(tmp_path):
    """临时 SQLite DB，预填充测试数据。每个测试函数独立 DB。"""
    db_path = str(tmp_path / "test_mem_meta.db")
    _init_db(db_path)
    yield db_path
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def mock_app(mock_db):
    """构建带 mem_meta 端点的 FastAPI app。"""
    return _build_app(mock_db)


@pytest_asyncio.fixture
async def client(mock_app):
    """ASGI 直连客户端（不占端口）。"""
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def reset_client(mock_app):
    """带自动重置的客户端：每个测试前重置数据。"""
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/admin/mem_meta/reset")
        yield c
