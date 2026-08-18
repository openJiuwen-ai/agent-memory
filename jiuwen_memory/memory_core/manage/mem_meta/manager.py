# -*- coding: UTF-8 -*-
"""MemMetaManager — 记忆元数据管理器

职责:
  1. 元数据刷新（通过内核 SimpleMemoryIndex 扫描 uid_* collection → 填充 av_user_stats）
  2. 批量删除（逐用户删除 blacklisted 过期记忆 → 更新 av_user_stats）
  3. 任务管理（创建/查询/防重/冷却/僵尸清理）

通过内核 BaseDbStore 抽象层操作数据库，适配 SQLite / GaussDB / PostgreSQL。
不建 global_summary 表，全局指标从 av_user_stats 聚合查询。
"""
import asyncio
import json
import logging
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column, Integer, String, Text, Index, MetaData, Table,
    select, insert, update, delete, func, case,
)
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# ============================================================
# 配置常量
# ============================================================

COOLDOWN_SECONDS = 300  # 5 分钟冷却
MAX_QUERY_LIMIT = 10**9  # 查询全部记忆的 limit 值
MAX_ERROR_MSG_LEN = 500  # error_message 最大存储长度
MAX_USER_ERROR_LEN = 300  # user_result error 最大长度
ZOMBIE_TASK_TIMEOUT_HOURS = 1  # 僵尸任务超时阈值


def _now_str() -> str:
    """返回当前 UTC 时间字符串（数据库兼容格式）。

    使用 Python 侧时间戳，兼容 SQLite / PostgreSQL / GaussDB。
    """
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# ORM 模型定义（通过 SQLAlchemy 自动适配不同数据库方言）
# ============================================================

_metadata = MetaData()

av_user_stats_table = Table(
    "av_user_stats", _metadata,
    Column("scope_user", String, primary_key=True),
    Column("total_count", Integer, nullable=False),
    Column("active_count", Integer, nullable=False),
    Column("superseded_count", Integer, nullable=False),
    Column("expired_30d_count", Integer, nullable=False),
    Column("expired_all_count", Integer, nullable=False),
    Column("tier_episodic", Integer, default=0),
    Column("tier_semantic", Integer, default=0),
    Column("tier_other", Integer, default=0),
    Column("created_at", String, nullable=False, default=_now_str),
    Column("updated_at", String, nullable=False, default=_now_str),
)

mem_meta_task_table = Table(
    "mem_meta_task", _metadata,
    Column("task_id", String, primary_key=True),
    Column("task_type", String, nullable=False),
    Column("status", String, nullable=False, default="pending"),
    Column("request_params", Text),
    Column("result_summary", Text),
    Column("error_message", Text),
    Column("total_users", Integer, default=0),
    Column("processed_users", Integer, default=0),
    Column("deleted_count", Integer, default=0),
    Column("failed_count", Integer, default=0),
    Column("created_at", String, nullable=False, default=_now_str),
    Column("updated_at", String, nullable=False, default=_now_str),
    Column("started_at", String),
    Column("finished_at", String),
    Index("idx_task_status", "status"),
    Index("idx_task_type_created", "task_type"),
)

AV_USER_STATS_COLS = list(av_user_stats_table.columns.keys())
MEM_META_TASK_COLS = list(mem_meta_task_table.columns.keys())


class MemMetaManager:
    """记忆元数据管理器。

    通过内核 BaseDbStore 抽象层操作数据库（适配 SQLite / GaussDB / PostgreSQL），
    提供 refresh / batch_delete / task_status 等异步接口。
    """

    def __init__(
        self,
        memory_engine: Any = None,
        db_store: Any = None,
    ):
        self.engine = memory_engine
        self.db_store = db_store
        # 防止 asyncio task 被 GC 回收
        self._background_tasks: set = set()
        self._init_db()

    @property
    def _engine(self) -> AsyncEngine:
        """获取 SQLAlchemy AsyncEngine。"""
        if self.db_store is None:
            raise RuntimeError("db_store is None, cannot access database")
        return self.db_store.get_async_engine()

    # ============================================================
    # 数据库初始化
    # ============================================================

    def _init_db(self) -> None:
        """初始化数据库，建 2 张表。"""
        if self.db_store is None:
            return
        engine = self.db_store.get_async_engine()
        try:
            loop = asyncio.get_running_loop()
            # 保存 task 引用防止 GC 回收
            task = loop.create_task(self._async_init_db())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            # 没有运行中的事件循环，用同步引擎建表
            import sqlalchemy
            sync_engine = sqlalchemy.create_engine(engine.engine.url)
            with sync_engine.begin() as conn:
                _metadata.create_all(conn, checkfirst=True)

    async def _async_init_db(self) -> None:
        """异步建表。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(_metadata.create_all, checkfirst=True)

    # ============================================================
    # 任务管理
    # ============================================================

    async def _check_task_guard(
        self, task_type: str, force: bool = False
    ) -> dict | None:
        """防重检查：运行中任务 + 5 分钟冷却。

        返回 None 表示可以提交新任务；
        返回 dict 表示存在冲突，包含已有任务信息。
        """
        if force:
            return None

        async with self._engine.begin() as conn:
            # 1. 检查运行中任务
            result = await conn.execute(
                select(mem_meta_task_table.c.task_id, mem_meta_task_table.c.status)
                .where(mem_meta_task_table.c.task_type == task_type)
                .where(mem_meta_task_table.c.status == "running")
                .order_by(mem_meta_task_table.c.created_at.desc())
                .limit(1)
            )
            row = result.fetchone()
            if row:
                return {
                    "task_id": row[0],
                    "status": row[1],
                    "message": "存在正在执行的任务",
                }

            # 2. 检查冷却（最新任务距今 < COOLDOWN_SECONDS）
            result = await conn.execute(
                select(
                    mem_meta_task_table.c.task_id,
                    mem_meta_task_table.c.status,
                    mem_meta_task_table.c.created_at,
                )
                .where(mem_meta_task_table.c.task_type == task_type)
                .order_by(mem_meta_task_table.c.created_at.desc())
                .limit(1)
            )
            row = result.fetchone()
            if row:
                task_id, status, created_at_str = row
                try:
                    created_at = datetime.strptime(
                        created_at_str, "%Y-%m-%d %H:%M:%S"
                    )
                    elapsed = (datetime.utcnow() - created_at).total_seconds()
                    if elapsed < COOLDOWN_SECONDS:
                        return {
                            "task_id": task_id,
                            "status": status,
                            "message": f"冷却期内（<{COOLDOWN_SECONDS}秒）",
                        }
                except ValueError as exc:
                    logger.warning(
                        "task guard: time parse failed for %s: %s",
                        created_at_str, exc)
            return None

    async def _create_task(
        self,
        task_type: str,
        request_params: dict | None = None,
        total_users: int = 0,
    ) -> str:
        """创建任务记录（INSERT mem_meta_task，status=pending）。"""
        task_id = str(uuid.uuid4())
        now = _now_str()
        params_json = (
            json.dumps(request_params, ensure_ascii=False)
            if request_params
            else None
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(mem_meta_task_table).values(
                    task_id=task_id,
                    task_type=task_type,
                    status="pending",
                    request_params=params_json,
                    total_users=total_users,
                    created_at=now,
                    updated_at=now,
                )
            )
        return task_id

    async def _update_task(self, task_id: str, **fields: Any) -> None:
        """更新任务字段（UPDATE mem_meta_task）。

        使用 Python 侧时间戳（_now_str），兼容 SQLite / PostgreSQL / GaussDB。
        """
        fields["updated_at"] = _now_str()
        async with self._engine.begin() as conn:
            await conn.execute(
                update(mem_meta_task_table)
                .where(mem_meta_task_table.c.task_id == task_id)
                .values(**fields)
            )

    def _create_background_task(self, coro) -> asyncio.Task:
        """创建后台任务并保持引用（防 GC 回收）。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def cleanup_zombie_tasks(self) -> None:
        """清理僵尸任务（running 超 1 小时 → failed）。"""
        cutoff = (
            datetime.utcnow() - timedelta(hours=ZOMBIE_TASK_TIMEOUT_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        async with self._engine.begin() as conn:
            await conn.execute(
                update(mem_meta_task_table)
                .where(mem_meta_task_table.c.status == "running")
                .where(mem_meta_task_table.c.started_at < cutoff)
                .values(
                    status="failed",
                    error_message="zombie task cleaned up on restart",
                    finished_at=_now_str(),
                    updated_at=_now_str(),
                )
            )

    # ============================================================
    # 接口 1: refresh（异步刷新元数据）
    # ============================================================

    async def submit_refresh(self, force: bool = False) -> dict:
        """提交元数据刷新任务。

        流程: 防重检查 → 创建任务 → asyncio.create_task 后台执行。
        返回 accepted（202）或 skipped（200）。
        """
        existing = await self._check_task_guard("refresh_meta", force=force)
        if existing:
            return {
                "status": "skipped",
                "task_type": "refresh_meta",
                "task_id": existing.get("task_id"),
                "task_status": existing.get("status"),
                "message": existing.get("message", ""),
            }
        task_id = await self._create_task(
            "refresh_meta", request_params={"force": force}
        )
        self._create_background_task(self._run_refresh_meta(task_id))
        return {
            "status": "accepted",
            "task_id": task_id,
            "task_type": "refresh_meta",
            "message": "元数据刷新任务已提交",
        }

    async def _run_refresh_meta(self, task_id: str) -> None:
        """后台执行: UPDATE running → DELETE av_user_stats
        → 通过内核 SimpleMemoryIndex 扫描 uid_* collection
        → 统计 blacklisted/expired → INSERT av_user_stats → UPDATE completed。

        扫描内核真实数据（uid_{user}_gid_{scope}_mtype_* collection + KV），
        而非旧平台的 agent_memory_vectors。
        """
        await self._update_task(
            task_id, status="running", started_at=_now_str()
        )
        try:
            engine = self.engine
            if engine is None or engine.memory_index is None:
                await self._update_task(
                    task_id,
                    status="failed",
                    finished_at=_now_str(),
                    error_message="memory_engine or memory_index is None",
                )
                return

            memory_index = engine.memory_index
            now_str = _now_str()
            now = datetime.utcnow()

            # 获取所有 (user_id, scope_id) 对
            all_scopes = await memory_index.list_user_scopes()

            user_stats: dict = defaultdict(
                lambda: {
                    "total": 0,
                    "active": 0,
                    "superseded": 0,
                    "expired_30d": 0,
                    "expired_all": 0,
                    "tiers": Counter(),
                }
            )

            total_scanned = 0
            for user_id, scope_id in all_scopes:
                try:
                    docs, _ = await memory_index.list_memories_with_total(
                        user_id, scope_id, offset=0, limit=MAX_QUERY_LIMIT,
                    )
                    s = user_stats[user_id]
                    for doc in docs:
                        total_scanned += 1
                        s["total"] += 1
                        # tier 统计（按 mem_type）
                        if doc.type:
                            s["tiers"][doc.type] += 1
                        # blacklisted = 过期
                        if doc.blacklisted:
                            s["superseded"] += 1
                            s["expired_all"] += 1
                            # 判断是否过期超过30天
                            if doc.timestamp:
                                days_ago = (now - doc.timestamp.replace(tzinfo=None)).days
                                if days_ago >= 30:
                                    s["expired_30d"] += 1
                            else:
                                s["expired_30d"] += 1
                        else:
                            s["active"] += 1
                except Exception as exc:
                    logger.warning(
                        "refresh: scope (%s, %s) failed: %s",
                        user_id, scope_id, exc)
                    continue

            # 填充 av_user_stats
            async with self._engine.begin() as conn:
                await conn.execute(delete(av_user_stats_table))
                for u, s in user_stats.items():
                    t = s["tiers"]
                    await conn.execute(
                        insert(av_user_stats_table).values(
                            scope_user=u,
                            total_count=s["total"],
                            active_count=s["active"],
                            superseded_count=s["superseded"],
                            expired_30d_count=s["expired_30d"],
                            expired_all_count=s["expired_all"],
                            tier_episodic=t.get("episodic_memory", 0),
                            tier_semantic=t.get("semantic_memory", 0),
                            tier_other=sum(
                                v for k, v in t.items()
                                if k not in ("episodic_memory", "semantic_memory")
                            ),
                            created_at=now_str,
                            updated_at=now_str,
                        )
                    )

            # UPDATE completed
            total_expired = sum(
                s["expired_30d"] for s in user_stats.values()
            )
            await self._update_task(
                task_id,
                status="completed",
                finished_at=_now_str(),
                result_summary=json.dumps(
                    {
                        "total_scanned": total_scanned,
                        "total_users": len(user_stats),
                        "expired_30d": total_expired,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            await self._update_task(
                task_id,
                status="failed",
                finished_at=_now_str(),
                error_message=str(e)[:MAX_ERROR_MSG_LEN],
            )

    # ============================================================
    # 接口 2: expired_memorys（同步查询）
    # ============================================================

    async def get_expired_memorys(
        self,
        inactive_days_threshold: int = 30,
        limit: int = 10,
        min_expired_count: int = 0,
    ) -> dict:
        """查询过期用户 Top N。

        inactive_days_threshold == 30: 走 av_user_stats 快表查询（快）
        inactive_days_threshold != 30: 走 KV 实时扫描（慢但准确）
        """
        # 查询最新 refresh 任务状态
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(mem_meta_task_table.c.task_id, mem_meta_task_table.c.status)
                .where(mem_meta_task_table.c.task_type == "refresh_meta")
                .order_by(mem_meta_task_table.c.created_at.desc())
                .limit(1)
            )
            task_row = result.fetchone()
            task_id = task_row[0] if task_row else None
            task_status = task_row[1] if task_row else "none"

        if inactive_days_threshold == 30:
            # === 快表路径：直接查 av_user_stats ===
            async with self._engine.begin() as conn:
                # 聚合全局指标
                result = await conn.execute(
                    select(func.count()).select_from(av_user_stats_table)
                )
                total_users = result.scalar()

                result = await conn.execute(
                    select(func.coalesce(func.sum(av_user_stats_table.c.expired_30d_count), 0))
                )
                total_expired_30d = result.scalar()

                result = await conn.execute(
                    select(func.count())
                    .select_from(av_user_stats_table)
                    .where(av_user_stats_table.c.expired_30d_count > 0)
                )
                users_with_expired = result.scalar()

                # 查询 Top N 用户
                result = await conn.execute(
                    select(av_user_stats_table)
                    .where(av_user_stats_table.c.expired_30d_count >= min_expired_count)
                    .order_by(av_user_stats_table.c.expired_30d_count.desc())
                    .limit(limit)
                )
                rows = result.fetchall()
                col_names = av_user_stats_table.columns.keys()
                users = [dict(zip(col_names, row)) for row in rows]
        else:
            # === 实时扫描路径：遍历 KV 统计 blacklisted 记忆 ===
            users = await self._scan_expired_users_realtime(
                inactive_days_threshold, limit, min_expired_count
            )
            total_users = len(users)
            total_expired_30d = sum(u.get("expired_30d_count", 0) for u in users)
            users_with_expired = sum(1 for u in users if u.get("expired_30d_count", 0) > 0)

        return {
            "status": "scanning" if task_status == "running" else "success",
            "task_id": task_id,
            "task_status": task_status,
            "inactive_days_threshold": inactive_days_threshold,
            "total_users": total_users,
            "total_expired_30d": total_expired_30d,
            "users_with_expired": users_with_expired,
            "top_users": users,
        }

    async def _scan_expired_users_realtime(
        self,
        inactive_days_threshold: int,
        limit: int,
        min_expired_count: int,
    ) -> list[dict]:
        """实时扫描 KV，统计每个用户的 blacklisted 记忆数。

        遍历所有 (user, scope) 对 → list_memories_with_total
        → 过滤 blacklisted → 按不活跃天数过滤 → 排序取 Top N。

        性能较慢，仅在 inactive_days_threshold != 30 时使用。
        """
        engine = self.engine
        if engine is None or engine.memory_index is None:
            return []

        memory_index = engine.memory_index
        all_scopes = await memory_index.list_user_scopes()

        # 按用户聚合统计
        now_dt = datetime.utcnow()
        user_stats: dict[str, dict] = {}
        for user_id, scope_id in all_scopes:
            try:
                docs, total = await memory_index.list_memories_with_total(
                    user_id, scope_id, offset=0, limit=MAX_QUERY_LIMIT,
                )
                # 过滤 blacklisted 且满足不活跃天数阈值
                expired_docs = [
                    d for d in docs
                    if d.blacklisted and (
                        d.timestamp is None
                        or (now_dt - d.timestamp.replace(
                            tzinfo=None)).days >= inactive_days_threshold
                    )
                ]
                expired_count = len(expired_docs)
                if expired_count == 0:
                    continue

                all_blacklisted = sum(1 for d in docs if d.blacklisted)
                if user_id not in user_stats:
                    user_stats[user_id] = {
                        "scope_user": user_id,
                        "total_count": 0,
                        "active_count": 0,
                        "superseded_count": 0,
                        "expired_30d_count": 0,
                        "expired_all_count": 0,
                    }
                s = user_stats[user_id]
                s["total_count"] += total
                s["active_count"] += sum(1 for d in docs if not d.blacklisted)
                s["superseded_count"] += all_blacklisted
                s["expired_30d_count"] += expired_count
                s["expired_all_count"] += all_blacklisted
            except Exception as exc:
                logger.warning(
                    "realtime scan: scope (%s, %s) failed: %s",
                    user_id, scope_id, exc)
                continue

        # 过滤 + 排序 + 取 Top N
        result = [
            u for u in user_stats.values()
            if u["expired_30d_count"] >= min_expired_count
        ]
        result.sort(key=lambda x: x["expired_30d_count"], reverse=True)
        return result[:limit]

    # ============================================================
    # 接口 3: batch_delete（异步批量删除）
    # ============================================================

    async def submit_batch_delete(
        self,
        user_ids: list[str] | None = None,
        all_expired: bool = False,
        inactive_days_threshold: int = 30,
        scope_id: str | None = None,
        cleanup_retrieve_history: bool = True,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict:
        """提交批量删除任务。

        流程: 查 expired users → 防重 → 创建任务 → asyncio 后台执行。
        """
        if all_expired:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(av_user_stats_table.c.scope_user)
                    .where(av_user_stats_table.c.expired_30d_count > 0)
                )
                user_ids = [r[0] for r in result.fetchall()]

        if not user_ids:
            return {
                "status": "success",
                "message": "无过期用户，无需删除",
                "total_users": 0,
            }

        existing = await self._check_task_guard("batch_delete", force=force)
        if existing:
            return {
                "status": "skipped",
                "task_type": "batch_delete",
                "task_id": existing.get("task_id"),
                "task_status": existing.get("status"),
                "message": existing.get("message", ""),
            }

        task_id = await self._create_task(
            "batch_delete",
            request_params={
                "user_ids": user_ids,
                "all_expired": all_expired,
                "inactive_days_threshold": inactive_days_threshold,
                "scope_id": scope_id,
                "cleanup_retrieve_history": cleanup_retrieve_history,
                "dry_run": dry_run,
            },
            total_users=len(user_ids),
        )
        self._create_background_task(
            self._run_batch_delete(
                task_id, user_ids, inactive_days_threshold,
                all_expired, scope_id, cleanup_retrieve_history, dry_run,
            )
        )
        return {
            "status": "accepted",
            "task_id": task_id,
            "task_type": "batch_delete",
            "message": f"批量删除任务已提交，涉及 {len(user_ids)} 个用户",
            "total_users": len(user_ids),
        }

    async def _run_batch_delete(
        self,
        task_id: str,
        user_ids: list[str],
        inactive_days_threshold: int = 30,
        all_expired: bool = False,
        scope_id: str | None = None,
        cleanup_retrieve_history: bool = True,
        dry_run: bool = False,
    ) -> None:
        """后台执行: 通过内核 SimpleMemoryIndex 精准删除 blacklisted=True 的过期记忆。

        逐用户遍历所有 scope → list_memories_with_total → 过滤 blacklisted
        → delete_memories(user_id, scope_id, mem_ids) 同步删 KV+Milvus
        → 更新 task 进度。
        """
        await self._update_task(
            task_id, status="running", started_at=_now_str()
        )
        try:
            # ★ 前置校验: 检查 refresh 任务是否在运行 ★
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(mem_meta_task_table.c.task_id)
                    .where(mem_meta_task_table.c.task_type == "refresh_meta")
                    .where(mem_meta_task_table.c.status.in_(["pending", "running"]))
                    .limit(1)
                )
                running_refresh = result.fetchone()

            if running_refresh:
                await self._update_task(
                    task_id,
                    status="failed",
                    finished_at=_now_str(),
                    error_message=(
                        f"refresh_meta task {running_refresh[0]} is still running, "
                        "batch_delete skipped"
                    ),
                )
                return

            engine = self.engine
            if engine is None or engine.memory_index is None:
                await self._update_task(
                    task_id,
                    status="failed",
                    finished_at=_now_str(),
                    error_message="memory_engine or memory_index is None",
                )
                return

            memory_index = engine.memory_index
            kv_store = engine.kv_store

            processed = 0
            deleted_total = 0
            failed = 0
            skipped = 0
            details: list[dict] = []

            for user_id in user_ids:
                user_result: dict = {
                    "user_id": user_id,
                    "total_deleted": 0,
                    "scopes_affected": 0,
                    "status": "success",
                    "error": None,
                }
                try:
                    from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock
                    async with DistributedLock(kv_store, f"user/{user_id}"):
                        # ★ 不活跃天数校验 (all_expired=true 时跳过校验) ★
                        if not all_expired:
                            if inactive_days_threshold == 30:
                                # === 快表路径：查 av_user_stats ===
                                async with self._engine.begin() as conn:
                                    result = await conn.execute(
                                        select(func.max(av_user_stats_table.c.expired_30d_count))
                                        .where(av_user_stats_table.c.scope_user == user_id)
                                    )
                                    row = result.fetchone()

                                if row is not None and row[0] is not None and row[0] == 0:
                                    # 用户在快表中且无过期记忆，跳过
                                    user_result["status"] = "skipped"
                                    user_result["error"] = (
                                        "expired_30d_count=0, no expired memories"
                                    )
                                    details.append(user_result)
                                    skipped += 1
                                    processed += 1
                                    await self._update_task(
                                        task_id,
                                        processed_users=processed,
                                        failed_count=failed,
                                    )
                                    continue
                                # row is None（用户不在快表中）或 row[0] > 0：
                                # 继续走实时扫描删除路径，不计 failed
                            # inactive_days_threshold != 30：直接走实时扫描删除路径

                        # 发现该用户所有 scope
                        if scope_id:
                            scopes = [(user_id, scope_id)]
                        else:
                            all_scopes = await memory_index.list_user_scopes()
                            scopes = [
                                (u, s) for (u, s) in all_scopes if u == user_id
                            ]

                        user_deleted = 0
                        for uid, sid in scopes:
                            try:
                                # 获取全部记忆
                                docs, _ = (
                                    await memory_index.list_memories_with_total(
                                        uid, sid,
                                        offset=0, limit=MAX_QUERY_LIMIT,
                                    )
                                )
                                # 过滤 blacklisted == True 且满足不活跃天数
                                now_dt = datetime.utcnow()
                                if all_expired:
                                    expired_mem_ids = [
                                        d.id for d in docs if d.blacklisted
                                    ]
                                else:
                                    expired_mem_ids = [
                                        d.id for d in docs
                                        if d.blacklisted and (
                                            d.timestamp is None
                                            or (now_dt - d.timestamp.replace(
                                                tzinfo=None)).days
                                            >= inactive_days_threshold
                                        )
                                    ]
                                if not expired_mem_ids:
                                    continue

                                if dry_run:
                                    user_deleted += len(expired_mem_ids)
                                    user_result["scopes_affected"] += 1
                                    continue

                                # 精准删除: KV + Milvus 同步删
                                await memory_index.delete_memories(
                                    uid, sid, expired_mem_ids
                                )

                                # 清理 retrieve_history
                                if cleanup_retrieve_history and kv_store:
                                    for mid in expired_mem_ids:
                                        rh_key = (
                                            f"retrieve_history"
                                            f"/{uid}/{sid}/{mid}"
                                        )
                                        try:
                                            await kv_store.delete(rh_key)
                                        except Exception as exc:
                                            logger.debug(
                                                "retrieve_history cleanup "
                                                "failed for %s: %s",
                                                rh_key, exc)

                                user_deleted += len(expired_mem_ids)
                                user_result["scopes_affected"] += 1
                            except Exception as e:
                                user_result["status"] = "partial_failed"
                                user_result["error"] = str(e)[:MAX_USER_ERROR_LEN]

                        user_result["total_deleted"] = user_deleted
                        deleted_total += user_deleted

                    processed += 1
                    await self._update_task(
                        task_id,
                        processed_users=processed,
                        deleted_count=deleted_total,
                    )

                    # ★ 同步更新 av_user_stats：删除了多少条就减多少 ★
                    if not dry_run and user_deleted > 0:
                        try:
                            async with self._engine.begin() as conn:
                                await conn.execute(
                                    update(av_user_stats_table)
                                    .where(av_user_stats_table.c.scope_user == user_id)
                                    .values(
                                        total_count=case(
                                            (av_user_stats_table.c.total_count >= user_deleted,
                                             av_user_stats_table.c.total_count - user_deleted),
                                            else_=0),
                                        superseded_count=case(
                                            (av_user_stats_table.c.superseded_count >= user_deleted,
                                             av_user_stats_table.c.superseded_count - user_deleted),
                                            else_=0),
                                        expired_30d_count=case(
                                            (av_user_stats_table.c.expired_30d_count >= user_deleted,
                                             av_user_stats_table.c.expired_30d_count - user_deleted),
                                            else_=0),
                                        expired_all_count=case(
                                            (av_user_stats_table.c.expired_all_count >= user_deleted,
                                             av_user_stats_table.c.expired_all_count - user_deleted),
                                            else_=0),
                                        updated_at=_now_str(),
                                    )
                                )
                        except Exception as exc:
                            logger.debug(
                                "av_user_stats update failed for "
                                "user %s: %s", user_id, exc)

                except Exception as e:
                    failed += 1
                    user_result["status"] = "failed"
                    user_result["error"] = str(e)[:MAX_USER_ERROR_LEN]
                    await self._update_task(
                        task_id,
                        failed_count=failed,
                        error_message=str(e)[:MAX_ERROR_MSG_LEN],
                    )

                details.append(user_result)

            # 最终状态判断
            if failed == 0:
                final_status = "completed"
            elif failed == len(user_ids):
                final_status = "failed"
            else:
                final_status = "partial_failed"
            await self._update_task(
                task_id,
                status=final_status,
                finished_at=_now_str(),
                result_summary=json.dumps(
                    {
                        "processed": processed,
                        "deleted": deleted_total,
                        "failed": failed,
                        "skipped": skipped,
                        "dry_run": dry_run,
                        "details": details,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            await self._update_task(
                task_id,
                status="failed",
                finished_at=_now_str(),
                error_message=str(e)[:MAX_ERROR_MSG_LEN],
            )

    # ============================================================
    # 接口 4: task_status（同步查询）
    # ============================================================

    async def get_task_status(self, task_id: str | None = None) -> dict:
        """查询任务状态。

        有 task_id: SELECT by task_id（不存在返回 not_found）。
        无 task_id: ORDER BY created_at DESC LIMIT 1（无记录返回 task=None）。
        """
        async with self._engine.begin() as conn:
            if task_id:
                result = await conn.execute(
                    select(mem_meta_task_table)
                    .where(mem_meta_task_table.c.task_id == task_id)
                )
                row = result.fetchone()
                if not row:
                    return {
                        "status": "not_found",
                        "task_id": task_id,
                        "message": f"任务 {task_id} 不存在",
                    }
            else:
                result = await conn.execute(
                    select(mem_meta_task_table)
                    .order_by(mem_meta_task_table.c.created_at.desc())
                    .limit(1)
                )
                row = result.fetchone()
                if not row:
                    return {
                        "status": "success",
                        "task": None,
                        "message": "无任务记录",
                    }
            col_names = mem_meta_task_table.columns.keys()
            task = dict(zip(col_names, row))
            return {"status": "success", "task": task}
