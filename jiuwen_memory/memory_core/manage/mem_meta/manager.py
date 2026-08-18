# -*- coding: UTF-8 -*-
"""MemMetaManager — Milvus 记忆元数据管理器

职责:
  1. 元数据刷新（扫描 Milvus agent_memory_vectors → 填充 av_user_stats）
  2. 批量删除（逐用户删除过期记忆 → 更新 av_user_stats）
  3. 任务管理（创建/查询/防重/冷却/僵尸清理）

不建 global_summary 表，全局指标从 av_user_stats 聚合查询。
"""
import asyncio
import base64
import json
import os
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

# ============================================================
# 配置常量
# ============================================================

MILVUS_URI = "http://localhost:8530"
COLLECTION = "agent_memory_vectors"
SENTINEL = 253402300799000  # 9999-12-31 哨兵值（active 记录的 t_invalid）
COOLDOWN_SECONDS = 300  # 5 分钟冷却
DB_PATH = "/tmp/milvus_memory_metadata.db"

# av_user_stats 表列（11 列）
AV_USER_STATS_COLS = [
    "scope_user", "total_count", "active_count", "superseded_count",
    "expired_30d_count", "expired_all_count",
    "tier_episodic", "tier_semantic", "tier_other",
    "created_at", "updated_at",
]

# mem_meta_task 表列（14 列）
MEM_META_TASK_COLS = [
    "task_id", "task_type", "status", "request_params", "result_summary",
    "error_message", "total_users", "processed_users", "deleted_count",
    "failed_count", "created_at", "updated_at", "started_at", "finished_at",
]

# _create_task 插入的列子集
_TASK_INSERT_COLS = [
    "task_id", "task_type", "status", "request_params",
    "total_users", "created_at", "updated_at",
]


def make_insert(table: str, columns: list[str]) -> str:
    """程序化生成 INSERT SQL，避免占位符计数错误。

    与 gen_metadata_db.py 中的辅助函数保持一致。
    """
    col_str = ",".join(columns)
    ph_str = ",".join(["?"] * len(columns))
    return f"INSERT INTO {table} ({col_str}) VALUES ({ph_str})"


def _decode_metadata(meta: Any) -> dict:
    """解码 Milvus metadata 字段（可能是 dict、base64 字符串或 JSON 字符串）。"""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str):
        try:
            return json.loads(base64.b64decode(meta).decode())
        except Exception:
            try:
                return json.loads(meta)
            except Exception:
                return {}
    return {}


def _now_str() -> str:
    """返回当前 UTC 时间字符串（SQLite 兼容格式）。"""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


class MemMetaManager:
    """Milvus 记忆元数据管理器。

    管理 SQLite 元数据库（av_user_stats + mem_meta_task），
    提供 refresh / batch_delete / task_status 等异步接口。
    """

    def __init__(
        self,
        memory_engine: Any = None,
        milvus_uri: str = MILVUS_URI,
        db_path: str = DB_PATH,
    ):
        self.engine = memory_engine
        self.milvus_uri = milvus_uri
        self.db_path = db_path
        # 防止 asyncio task 被 GC 回收
        self._background_tasks: set = set()
        self._init_db()

    # ============================================================
    # 数据库初始化
    # ============================================================

    def _init_db(self) -> None:
        """初始化 SQLite 数据库，建 2 张表（不建 global_summary）。"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # 表1: av_user_stats（11 列）
        cur.execute(
            "CREATE TABLE IF NOT EXISTS av_user_stats ("
            "  scope_user        TEXT PRIMARY KEY,"
            "  total_count        INTEGER NOT NULL,"
            "  active_count       INTEGER NOT NULL,"
            "  superseded_count   INTEGER NOT NULL,"
            "  expired_30d_count  INTEGER NOT NULL,"
            "  expired_all_count  INTEGER NOT NULL,"
            "  tier_episodic      INTEGER DEFAULT 0,"
            "  tier_semantic      INTEGER DEFAULT 0,"
            "  tier_other         INTEGER DEFAULT 0,"
            "  created_at         TEXT NOT NULL DEFAULT (datetime('now')),"
            "  updated_at         TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        # 表2: mem_meta_task（14 列）
        cur.execute(
            "CREATE TABLE IF NOT EXISTS mem_meta_task ("
            "  task_id          TEXT PRIMARY KEY,"
            "  task_type         TEXT NOT NULL,"
            "  status            TEXT NOT NULL DEFAULT 'pending',"
            "  request_params    TEXT,"
            "  result_summary    TEXT,"
            "  error_message     TEXT,"
            "  total_users       INTEGER DEFAULT 0,"
            "  processed_users   INTEGER DEFAULT 0,"
            "  deleted_count     INTEGER DEFAULT 0,"
            "  failed_count      INTEGER DEFAULT 0,"
            "  created_at        TEXT NOT NULL DEFAULT (datetime('now')),"
            "  updated_at        TEXT NOT NULL DEFAULT (datetime('now')),"
            "  started_at        TEXT,"
            "  finished_at       TEXT"
            ")"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_status "
            "ON mem_meta_task(status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_type_created "
            "ON mem_meta_task(task_type, created_at DESC)"
        )
        conn.commit()
        conn.close()

    # ============================================================
    # 任务管理
    # ============================================================

    def _check_task_guard(
        self, task_type: str, force: bool = False
    ) -> dict | None:
        """防重检查：运行中任务 + 5 分钟冷却。

        返回 None 表示可以提交新任务；
        返回 dict 表示存在冲突，包含已有任务信息。
        """
        if force:
            return None

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            # 1. 检查运行中任务
            cur.execute(
                "SELECT task_id, status FROM mem_meta_task "
                "WHERE task_type=? AND status='running' "
                "ORDER BY created_at DESC LIMIT 1",
                (task_type,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "task_id": row[0],
                    "status": row[1],
                    "message": "存在正在执行的任务",
                }

            # 2. 检查冷却（最新任务距今 < COOLDOWN_SECONDS）
            cur.execute(
                "SELECT task_id, status, created_at FROM mem_meta_task "
                "WHERE task_type=? ORDER BY created_at DESC LIMIT 1",
                (task_type,),
            )
            row = cur.fetchone()
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
                except ValueError:
                    pass
            return None
        finally:
            conn.close()

    def _create_task(
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
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        sql = make_insert("mem_meta_task", _TASK_INSERT_COLS)
        cur.execute(
            sql,
            (
                task_id,
                task_type,
                "pending",
                params_json,
                total_users,
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return task_id

    def _update_task(self, task_id: str, **fields: Any) -> None:
        """更新任务字段（UPDATE mem_meta_task）。"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=datetime('now')")
        vals.append(task_id)
        cur.execute(
            f"UPDATE mem_meta_task SET {','.join(sets)} WHERE task_id=?",
            vals,
        )
        conn.commit()
        conn.close()

    def _create_background_task(self, coro) -> asyncio.Task:
        """创建后台任务并保持引用（防 GC 回收）。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def cleanup_zombie_tasks(self) -> None:
        """清理僵尸任务（running 超 1 小时 → failed）。"""
        cutoff = (
            datetime.utcnow() - timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "UPDATE mem_meta_task SET status='failed', "
            "error_message='zombie task cleaned up on restart', "
            "finished_at=datetime('now'), updated_at=datetime('now') "
            "WHERE status='running' AND started_at < ?",
            (cutoff,),
        )
        conn.commit()
        conn.close()

    # ============================================================
    # 接口 1: refresh（异步刷新元数据）
    # ============================================================

    async def submit_refresh(self, force: bool = False) -> dict:
        """提交元数据刷新任务。

        流程: 防重检查 → 创建任务 → asyncio.create_task 后台执行。
        返回 accepted（202）或 skipped（200）。
        """
        existing = self._check_task_guard("refresh_meta", force=force)
        if existing:
            # 不用 **existing 展开，避免其 status 字段覆盖 "skipped"
            return {
                "status": "skipped",
                "task_type": "refresh_meta",
                "task_id": existing.get("task_id"),
                "task_status": existing.get("status"),
                "message": existing.get("message", ""),
            }
        task_id = self._create_task(
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
        self._update_task(
            task_id, status="running", started_at=_now_str()
        )
        try:
            engine = self.engine
            if engine is None or engine.memory_index is None:
                self._update_task(
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
                    docs, total = await memory_index.list_memories_with_total(
                        user_id, scope_id, offset=0, limit=10**9,
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
                except Exception:
                    continue

            # 填充 av_user_stats
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM av_user_stats")
            av_sql = make_insert("av_user_stats", AV_USER_STATS_COLS)
            for u, s in user_stats.items():
                t = s["tiers"]
                cur.execute(
                    av_sql,
                    (
                        u,
                        s["total"],
                        s["active"],
                        s["superseded"],
                        s["expired_30d"],
                        s["expired_all"],
                        t.get("episodic_memory", 0),
                        t.get("semantic_memory", 0),
                        sum(
                            v
                            for k, v in t.items()
                            if k not in ("episodic_memory", "semantic_memory")
                        ),
                        now_str,
                        now_str,
                    ),
                )
            conn.commit()
            conn.close()

            # UPDATE completed
            total_expired = sum(
                s["expired_30d"] for s in user_stats.values()
            )
            self._update_task(
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
            self._update_task(
                task_id,
                status="failed",
                finished_at=_now_str(),
                error_message=str(e)[:500],
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
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT task_id, status FROM mem_meta_task "
                "WHERE task_type='refresh_meta' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            task_row = cur.fetchone()
            task_id = task_row[0] if task_row else None
            task_status = task_row[1] if task_row else "none"

            if inactive_days_threshold == 30:
                # === 快表路径：直接查 av_user_stats ===
                cur.execute("SELECT COUNT(*) FROM av_user_stats")
                total_users = cur.fetchone()[0]
                cur.execute(
                    "SELECT COALESCE(SUM(expired_30d_count), 0) FROM av_user_stats"
                )
                total_expired_30d = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM av_user_stats WHERE expired_30d_count > 0"
                )
                users_with_expired = cur.fetchone()[0]

                cur.execute(
                    "SELECT scope_user, total_count, active_count, superseded_count, "
                    "expired_30d_count, expired_all_count, "
                    "tier_episodic, tier_semantic, tier_other, updated_at "
                    "FROM av_user_stats WHERE expired_30d_count >= ? "
                    "ORDER BY expired_30d_count DESC LIMIT ?",
                    (min_expired_count, limit),
                )
                users = [
                    {
                        "scope_user": r[0],
                        "total_count": r[1],
                        "active_count": r[2],
                        "superseded_count": r[3],
                        "expired_30d_count": r[4],
                        "expired_all_count": r[5],
                        "tier_episodic": r[6],
                        "tier_semantic": r[7],
                        "tier_other": r[8],
                        "updated_at": r[9],
                    }
                    for r in cur.fetchall()
                ]
            else:
                # === 实时扫描路径：遍历 KV 统计 blacklisted 记忆 ===
                users = await self._scan_expired_users_realtime(
                    inactive_days_threshold, limit, min_expired_count
                )
                total_users = len(users)
                total_expired_30d = sum(u["expired_30d_count"] for u in users)
                users_with_expired = sum(1 for u in users if u["expired_30d_count"] > 0)
        finally:
            conn.close()

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
        user_stats: dict[str, dict] = {}
        for user_id, scope_id in all_scopes:
            try:
                docs, total = await memory_index.list_memories_with_total(
                    user_id, scope_id, offset=0, limit=10**9,
                )
                blacklisted_count = sum(1 for d in docs if d.blacklisted)
                if blacklisted_count == 0:
                    continue

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
                s["superseded_count"] += blacklisted_count
                s["expired_30d_count"] += blacklisted_count
                s["expired_all_count"] += blacklisted_count
            except Exception:
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
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT scope_user FROM av_user_stats WHERE expired_30d_count > 0"
            )
            user_ids = [r[0] for r in cur.fetchall()]
            conn.close()

        if not user_ids:
            return {
                "status": "success",
                "message": "无过期用户，无需删除",
                "total_users": 0,
            }

        existing = self._check_task_guard("batch_delete", force=force)
        if existing:
            return {
                "status": "skipped",
                "task_type": "batch_delete",
                "task_id": existing.get("task_id"),
                "task_status": existing.get("status"),
                "message": existing.get("message", ""),
            }

        task_id = self._create_task(
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
        self._update_task(
            task_id, status="running", started_at=_now_str()
        )
        try:
            # ★ 前置校验: 检查 refresh 任务是否在运行 ★
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT task_id FROM mem_meta_task "
                "WHERE task_type='refresh_meta' AND status IN ('pending', 'running') "
                "LIMIT 1"
            )
            running_refresh = cur.fetchone()
            conn.close()
            if running_refresh:
                self._update_task(
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
                self._update_task(
                    task_id,
                    status="failed",
                    finished_at=_now_str(),
                    error_message="memory_engine or memory_index is None",
                )
                return

            memory_index = engine.memory_index
            kv_store = engine.kv_store

            from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock

            processed = 0
            deleted_total = 0
            failed = 0
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
                    async with DistributedLock(kv_store, f"user/{user_id}"):
                        # ★ 不活跃天数校验 (all_expired=true 时跳过) ★
                        if not all_expired:
                            if inactive_days_threshold == 30:
                                # === 快表路径：查 av_user_stats ===
                                conn = sqlite3.connect(self.db_path)
                                cur = conn.cursor()
                                cur.execute(
                                    "SELECT MAX(expired_30d_count) FROM av_user_stats "
                                    "WHERE scope_user = ?",
                                    (user_id,),
                                )
                                row = cur.fetchone()
                                conn.close()
                                if row is None or row[0] is None:
                                    user_result["status"] = "skipped"
                                    user_result["error"] = (
                                        "not found in av_user_stats, skipped"
                                    )
                                    details.append(user_result)
                                    failed += 1
                                    self._update_task(
                                        task_id,
                                        failed_count=failed,
                                        processed_users=processed + 1,
                                    )
                                    processed += 1
                                    continue
                                if row[0] == 0:
                                    user_result["status"] = "skipped"
                                    user_result["error"] = (
                                        f"expired_30d_count=0, no expired memories"
                                    )
                                    details.append(user_result)
                                    processed += 1
                                    self._update_task(
                                        task_id,
                                        processed_users=processed,
                                    )
                                    continue
                            else:
                                # === 实时扫描路径：不查快表，直接遍历 scope 删 ===
                                # inactive_days_threshold != 30 时，
                                # 快表数据（固定30天阈值）不适用，
                                # 跳过校验，直接进入下面的遍历 scope 删除逻辑
                                pass

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
                                docs, total = (
                                    await memory_index.list_memories_with_total(
                                        uid, sid,
                                        offset=0, limit=10**9,
                                    )
                                )
                                # 过滤 blacklisted == True
                                expired_mem_ids = [
                                    d.id for d in docs if d.blacklisted
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
                                        except Exception:
                                            pass  # 检索历史清理失败不影响主流程

                                user_deleted += len(expired_mem_ids)
                                user_result["scopes_affected"] += 1
                            except Exception as e:
                                user_result["status"] = "partial_failed"
                                user_result["error"] = str(e)[:300]

                        user_result["total_deleted"] = user_deleted
                        deleted_total += user_deleted

                    processed += 1
                    self._update_task(
                        task_id,
                        processed_users=processed,
                        deleted_count=deleted_total,
                    )

                    # ★ 同步更新 av_user_stats：删除了多少条就减多少 ★
                    if not dry_run and user_deleted > 0:
                        try:
                            conn = sqlite3.connect(self.db_path)
                            cur = conn.cursor()
                            now_str = _now_str()
                            cur.execute(
                                "UPDATE av_user_stats SET "
                                "  total_count = MAX(0, total_count - ?), "
                                "  superseded_count = MAX(0, superseded_count - ?), "
                                "  expired_30d_count = MAX(0, expired_30d_count - ?), "
                                "  expired_all_count = MAX(0, expired_all_count - ?), "
                                "  updated_at = ? "
                                "WHERE scope_user = ?",
                                (user_deleted, user_deleted,
                                 user_deleted, user_deleted,
                                 now_str, user_id),
                            )
                            conn.commit()
                            conn.close()
                        except Exception:
                            pass  # av_user_stats 更新失败不影响删除主流程
                except Exception as e:
                    failed += 1
                    user_result["status"] = "failed"
                    user_result["error"] = str(e)[:300]
                    self._update_task(
                        task_id,
                        failed_count=failed,
                        error_message=str(e)[:500],
                    )

                details.append(user_result)

            # 最终状态判断
            if failed == 0:
                final_status = "completed"
            elif failed == len(user_ids):
                final_status = "failed"
            else:
                final_status = "partial_failed"
            self._update_task(
                task_id,
                status=final_status,
                finished_at=_now_str(),
                result_summary=json.dumps(
                    {
                        "processed": processed,
                        "deleted": deleted_total,
                        "failed": failed,
                        "dry_run": dry_run,
                        "details": details,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            self._update_task(
                task_id,
                status="failed",
                finished_at=_now_str(),
                error_message=str(e)[:500],
            )

    # ============================================================
    # 接口 4: task_status（同步查询）
    # ============================================================

    async def get_task_status(self, task_id: str | None = None) -> dict:
        """查询任务状态。

        有 task_id: SELECT by task_id（不存在返回 not_found）。
        无 task_id: ORDER BY created_at DESC LIMIT 1（无记录返回 task=None）。
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            if task_id:
                cur.execute(
                    "SELECT * FROM mem_meta_task WHERE task_id=?", (task_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "status": "not_found",
                        "task_id": task_id,
                        "message": f"任务 {task_id} 不存在",
                    }
            else:
                cur.execute(
                    "SELECT * FROM mem_meta_task ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "status": "success",
                        "task": None,
                        "message": "无任务记录",
                    }
            task = dict(zip(MEM_META_TASK_COLS, row))
            return {"status": "success", "task": task}
        finally:
            conn.close()
