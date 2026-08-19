# -*- coding: UTF-8 -*-
"""mem_meta_api.py — 批量删除管理 API 路由

路由前缀: /admin/mem_meta
注册方式: register_mem_meta_endpoints(app, memory_engine=None, ...)

端点:
  POST /refresh          → 202 + task_id（异步刷新元数据）
  POST /expired_memorys  → 200 + top_users（同步查询过期用户）
  POST /batch_delete     → 202 + task_id（异步批量删除）
  GET  /task_status       → 200 + task 详情 或 404
"""
import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# 模块级 manager 引用 — 由 register_mem_meta_endpoints() 注入
# ============================================================
_manager = None
_background_tasks: set = set()

# ============================================================
# APIRouter
# ============================================================
router = APIRouter(prefix="/admin/mem_meta", tags=["批量删除管理"])


# ============================================================
# 请求模型（Pydantic v2，使用 ConfigDict）
# ============================================================

class RefreshRequest(BaseModel):
    """刷新元数据请求"""
    model_config = ConfigDict(extra="forbid")

    force: bool = False  # true=跳过冷却检查强制刷新


class ExpiredMemorysRequest(BaseModel):
    """查询过期用户 Top N 请求"""
    model_config = ConfigDict(extra="forbid")

    inactive_days_threshold: int = Field(default=30, ge=1, le=365)  # 不活跃天数阈值
    limit: int = Field(default=10, ge=1, le=100)  # 返回 Top N，默认 10
    min_expired_count: int = Field(default=0, ge=0)  # 最小过期记忆数过滤


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    model_config = ConfigDict(extra="forbid")

    user_ids: Optional[list[str]] = None  # 指定用户列表
    all_expired: bool = False  # true=删除所有过期用户（无视 inactive_days_threshold）
    inactive_days_threshold: int = Field(default=30, ge=1, le=365)  # 不活跃天数阈值，删除前二次校验
    scope_id: Optional[str] = None  # 可选，限定只处理该 scope
    cleanup_retrieve_history: bool = True  # 是否清理检索历史
    dry_run: bool = False  # true=只统计不删除
    force: bool = False  # true=跳过冷却检查强制执行


# ============================================================
# 注册函数
# ============================================================

def register_mem_meta_endpoints(
    app,
    memory_engine=None,
    db_store=None,
):
    """注册批量删除管理端点到 FastAPI app。

    参数:
      app: FastAPI 应用实例
      memory_engine: LongTermMemory 单例（用于复用内核能力）
      db_store: BaseDbStore 实例（适配 SQLite/GaussDB/PostgreSQL）
    """
    global _manager
    from jiuwen_memory.memory_core.manage.mem_meta.manager import MemMetaManager
    _manager = MemMetaManager(
        memory_engine=memory_engine,
        db_store=db_store,
    )
    # 启动时清理僵尸任务（忽略事件循环不可用的情况）
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_manager.cleanup_zombie_tasks())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # 无运行中事件循环时跳过
        pass
    app.include_router(router)


# ============================================================
# 路由处理
# ============================================================

@router.post("/refresh")
async def refresh_meta(req: RefreshRequest):
    """1. 异步刷新元数据 → 202 accepted 或 200 skipped"""
    result = await _manager.submit_refresh(force=req.force)
    if result.get("status") == "accepted":
        return Response(
            status_code=202,
            content=json.dumps(result, ensure_ascii=False),
            media_type="application/json",
        )
    return result  # 200 skipped


@router.post("/expired_memorys")
async def get_expired_memorys(req: ExpiredMemorysRequest):
    """2. 查询过期用户 Top N（同步查询）"""
    return await _manager.get_expired_memorys(
        inactive_days_threshold=req.inactive_days_threshold,
        limit=req.limit, min_expired_count=req.min_expired_count
    )


@router.post("/batch_delete")
async def batch_delete(req: BatchDeleteRequest):
    """3. 异步批量删除用户过期记忆 → 202 accepted"""
    if not req.user_ids and not req.all_expired:
        raise HTTPException(
            status_code=422,
            detail="必须指定 user_ids 或 all_expired=true",
        )
    from jiuwen_memory.memory_core.manage.mem_meta.manager import BatchDeleteParams
    result = await _manager.submit_batch_delete(
        BatchDeleteParams(
            user_ids=req.user_ids,
            all_expired=req.all_expired,
            inactive_days_threshold=req.inactive_days_threshold,
            scope_id=req.scope_id,
            cleanup_retrieve_history=req.cleanup_retrieve_history,
            dry_run=req.dry_run,
            force=req.force,
        )
    )
    if result.get("status") == "accepted":
        return Response(
            status_code=202,
            content=json.dumps(result, ensure_ascii=False),
            media_type="application/json",
        )
    return result  # 200 skipped 或 success


@router.get("/task_status")
async def get_task_status(task_id: Optional[str] = None):
    """4. 查询任务状态 → 200 或 404"""
    result = await _manager.get_task_status(task_id=task_id)
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=404,
            detail=result.get("message", "任务不存在"),
        )
    return result
