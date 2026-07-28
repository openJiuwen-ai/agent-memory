# -*- coding: UTF-8 -*-
"""日志中心 API 模块 — 抽离自 memory_server.py

职责：
  - 运行日志端点（设计文档 §6.3.2 + KR-LOG-01/02）：/logs/tail, /logs/download, /logs/files
  - 用户消息日志管理端 API（KR-MSG-01~04，设计文档 §6.6.4/§6.6.5）：
    /admin/messages/query, /admin/messages/stats, /admin/messages/export,
    /admin/messages/detail/{msg_id}
  - 消息反查端点：/get_message_by_id/

由 memory_server.py 调用 register_log_center_endpoints(app, memory_engine=...) 注册到 FastAPI app。
"""
import csv
import io
import json
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from jiuwen_memory.memory_core.long_term_memory import LongTermMemory


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class GetMessageByIdRequest(BaseModel):
    message_id: str


class AdminMessageQueryRequest(BaseModel):
    scope_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    start_time: Optional[str] = None   # ISO 8601
    end_time: Optional[str] = None     # ISO 8601
    page_idx: int = 1                  # 1-based page index
    page_size: int = 20
    exclude_scopes: Optional[List[str]] = None  # 排除指定 scope（如中间产物 middle_term_memory）


class MessageExportParams(BaseModel):
    """Encapsulates the correlated query parameters for message export.

    Introduced to satisfy G.FNM.03 (too many arguments) by grouping the
    six related filter/pagination parameters into a single named container.
    """

    scope_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    limit: int = 5000


# ---------------------------------------------------------------------------
# 模块级依赖引用 — 由 register_log_center_endpoints() 注入
# ---------------------------------------------------------------------------

_memory_engine = None  # LongTermMemory 单例


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _message_store():
    """Return the kernel SqlMessageStore, or None if the engine is not ready."""
    if _memory_engine is None or getattr(_memory_engine, "message_manager", None) is None:
        return None
    return getattr(_memory_engine.message_manager, "_store", None)


def _message_to_dict(msg, metadata) -> Dict[str, Any]:
    ts = getattr(metadata, "timestamp", None)
    content = msg.content
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            content = str(content)
    return {
        "message_id": getattr(metadata, "message_id", None),
        "user_id": getattr(metadata, "user_id", None),
        "scope_id": getattr(metadata, "scope_id", None),
        "session_id": getattr(metadata, "session_id", None),
        "role": getattr(msg, "role", ""),
        "content": content,
        "timestamp": ts.isoformat() if ts else None,
    }


# 内核日志目录：默认 ./logs/（与 common/logging/default/constant.py DEFAULT_INNER_LOG_CONFIG 一致）
def _get_kernel_log_dir() -> Path:
    """获取内核日志目录，优先读 LOG_PATH 环境变量，回退默认 ./logs/。"""
    log_path = os.getenv("LOG_PATH", "./logs/")
    return Path(log_path).expanduser().resolve()


def _get_kernel_log_files() -> List[Path]:
    """获取内核日志目录下所有 .log 文件（含轮转文件），按修改时间倒序。"""
    log_dir = _get_kernel_log_dir()
    if not log_dir.exists():
        return []
    log_files = sorted(
        [f for f in log_dir.rglob("*.log*") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return log_files


# ---------------------------------------------------------------------------
# APIRouter — 日志中心 + 消息日志 + 审计 + 消息反查 端点
# ---------------------------------------------------------------------------

router = APIRouter()


# ===== 消息反查端点 =====

@router.post("/get_message_by_id/")
async def get_message_by_id_endpoint(request: GetMessageByIdRequest):
    """按消息 id 反查原始消息（role/content/timestamp），用于从记忆的 source_id 回溯来源对话。"""
    try:
        result = await _memory_engine.get_message_by_id(request.message_id)
        if result is None:
            return {"found": False, "message_id": request.message_id}
        msg, ts = result
        return {
            "found": True,
            "message_id": request.message_id,
            "role": msg.role,
            "content": msg.content,
            "timestamp": ts.isoformat() if ts else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting message: {str(e)}") from e


# ===== 用户消息日志管理端 API（KR-MSG-01~04，设计文档 §6.6.4/§6.6.5）=====
# 说明：/admin/* 端点遵循 auth_middleware 通用策略（GET 放行；POST/PUT/DELETE 在配置 KEY 时校验 Bearer）。

@router.post("/admin/messages/query")          # KR-MSG-01
async def admin_messages_query(request: AdminMessageQueryRequest):
    """管理端：用户消息日志分页查询（scope/user/session + 时间范围过滤 + offset 分页）。"""
    store = _message_store()
    if store is None:
        raise HTTPException(status_code=503, detail="memory engine not initialized")
    try:
        page_idx = max(1, int(request.page_idx))
        page_size = max(1, min(int(request.page_size), 200))
        message_filter = {
            "scope_id": request.scope_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "exclude_scopes": request.exclude_scopes,
        }
        message_filter = {k: v for k, v in message_filter.items() if v is not None}
        total = await store.count_messages(message_filter)
        rows = await store.get_messages(
            message_filter,
            limit=page_size,
            order_by="timestamp",
            order_direction="desc",
            offset=(page_idx - 1) * page_size,
        )
        items = [_message_to_dict(msg, meta) for msg, meta in rows]
        return {"total": total, "page_idx": page_idx, "page_size": page_size, "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying messages: {str(e)}") from e


@router.get("/admin/messages/stats")           # KR-MSG-02
async def admin_messages_stats(scope_id: Optional[str] = None, user_id: Optional[str] = None,
                               session_id: Optional[str] = None,
                               start_time: Optional[str] = None, end_time: Optional[str] = None):
    """管理端：用户消息日志统计（total + 按 role 聚合）。"""
    store = _message_store()
    if store is None:
        raise HTTPException(status_code=503, detail="memory engine not initialized")
    try:
        message_filter = {
            "scope_id": scope_id,
            "user_id": user_id,
            "session_id": session_id,
            "start_time": start_time,
            "end_time": end_time,
        }
        message_filter = {k: v for k, v in message_filter.items() if v is not None}
        total = await store.count_messages(message_filter)
        by_role = await store.count_by_role(message_filter)
        return {"total": total, "by_role": by_role}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error counting messages: {str(e)}") from e


@router.get("/admin/messages/export")          # KR-MSG-03
async def admin_messages_export(params: MessageExportParams = Depends()):
    """管理端：用户消息日志导出（CSV，上限保护 limit<=20000）。

    Query parameters are grouped into ``MessageExportParams`` to satisfy
    G.FNM.03 (too many arguments).
    """
    store = _message_store()
    if store is None:
        raise HTTPException(status_code=503, detail="memory engine not initialized")
    try:
        export_limit = max(1, min(int(params.limit), 20000))
        message_filter = {
            "scope_id": params.scope_id,
            "user_id": params.user_id,
            "session_id": params.session_id,
            "start_time": params.start_time,
            "end_time": params.end_time,
        }
        message_filter = {k: v for k, v in message_filter.items() if v is not None}
        rows = await store.get_messages(
            message_filter, limit=export_limit, order_by="timestamp", order_direction="asc",
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["message_id", "user_id", "scope_id", "session_id", "role", "timestamp", "content"])
        for msg, meta in rows:
            item = _message_to_dict(msg, meta)
            writer.writerow([
                item["message_id"], item["user_id"], item["scope_id"],
                item["session_id"], item["role"], item["timestamp"], item["content"],
            ])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=messages_export.csv"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exporting messages: {str(e)}") from e


@router.get("/admin/messages/detail/{msg_id}")  # KR-MSG-04
async def admin_messages_detail(msg_id: str):
    """管理端：单条用户消息详情（含完整元数据 user/scope/session）。"""
    if _memory_engine is None or getattr(_memory_engine, "message_manager", None) is None:
        raise HTTPException(status_code=503, detail="memory engine not initialized")
    try:
        result = await _memory_engine.message_manager.get_message_meta_by_id(msg_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"message {msg_id} not found")
        msg, meta = result
        return _message_to_dict(msg, meta)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting message detail: {str(e)}") from e


# ---------------------------------------------------------------------------
# 日志中心端点（设计文档 §6.3.2 + §7.13.1 KR-LOG-01/02，P0 必须）
# 运行日志不入库存储，服务层通过 HTTP 调用内核接口获取日志。
# ---------------------------------------------------------------------------

@router.get("/logs/tail")
async def logs_tail(lines: int = 200, level: Optional[str] = None, event_type: Optional[str] = None):
    """内核读取自己的运行日志，返回最近 N 行（设计文档 §6.3.2 + KR-LOG-01）。

    参数：
    - lines: 返回最近 N 行（默认 200，上限 5000）
    - level: 日志级别过滤（DEBUG/INFO/WARNING/ERROR，可选）
    - event_type: 事件类型过滤（如 MEMORY_INIT/MEMORY_STORE，可选）
    """
    try:
        lines = max(1, min(int(lines), 5000))
        log_files = _get_kernel_log_files()
        if not log_files:
            return {"lines": [], "total": 0, "message": "no log files found"}

        # P1-2 Fix: 使用 collections.deque(maxlen=lines) 流式逐行读取，
        # 避免将整个大日志文件加载到内存导致 OOM。
        # deque(maxlen=N) 自动保留最后 N 条匹配行，内存占用恒定为 O(N)。
        collected: deque = deque(maxlen=lines)
        for log_file in log_files:
            if len(collected) >= lines:
                break
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        # 安全修复（原子串匹配会误判正文含 INFO/ERROR 的行）：
                        # level 采用日志级别锚定匹配（[LEVEL] / LEVEL| / LEVEL: / LEVEL空格），
                        # 且仅在该行确实包含级别标记时才过滤，避免把消息正文误判为级别。
                        if level:
                            lvl = level.upper()
                            if not re.search(rf"\[{lvl}\]|\b{lvl}\b\s*[|:\-]", line):
                                continue
                        if event_type and event_type.upper() not in line.upper():
                            continue
                        collected.append(line.rstrip("\n"))
            except (OSError, IOError):
                continue

        # deque(maxlen=lines) 已保证不超过 lines 行，直接转为 list 返回
        result = list(collected)
        return {
            "lines": result,
            "total": len(result),
            "log_dir": str(_get_kernel_log_dir()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading logs tail: {str(e)}") from e


@router.get("/logs/download")
async def logs_download(filename: str):
    """内核按文件名下载指定日志文件（设计文档 §6.3.2 + KR-LOG-02，先查询后下载模式）。

    参数：
    - filename: 日志文件相对路径（由 /logs/files 返回的 filename 字段，如 run/jiuwen.log）

    安全：严格校验 filename 必须在内核日志目录内，防止目录穿越攻击。
    返回：文件字节流（application/octet-stream），Content-Disposition 带原始文件名。
    """
    # P1-3 Fix: 使用 FileResponse 流式传输文件，避免 read_bytes() 将整个日志文件加载到内存导致 OOM。
    # FileResponse 内部使用分块读取 + chunked transfer encoding，内存占用恒定。
    try:
        if not filename or not filename.strip():
            raise HTTPException(status_code=400, detail="filename parameter is required")

        log_dir = _get_kernel_log_dir()
        # 安全校验：拼接后必须在日志目录内，防止 ../../etc/passwd 等穿越
        target = (log_dir / filename).resolve()
        try:
            target.relative_to(log_dir)
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"filename '{filename}' is outside kernel log directory",
            ) from exc

        if not target.exists() or not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"log file '{filename}' not found",
            )

        # 校验文件扩展名（只允许 .log 及轮转文件 .log.*）
        if not (target.name.endswith(".log") or ".log." in target.name):
            raise HTTPException(
                status_code=400,
                detail=f"filename '{filename}' is not a valid log file",
            )

        # 使用纯文件名作为下载文件名（去掉目录层级）
        download_name = target.name
        return FileResponse(
            path=str(target),
            media_type="application/octet-stream",
            filename=download_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error downloading log file: {str(e)}") from e


@router.get("/logs/files")
async def logs_files():
    """列出内核日志目录下所有可下载的日志文件（设计文档 §6.3.2，先查询后下载模式）。

    复用 _get_kernel_log_files()，返回每个文件的：
    - filename: 相对于日志目录的路径（如 run/jiuwen.log）
    - log_type: 日志类型（common/interface/prompt_builder/performance，按路径推断）
    - size_bytes: 文件大小（字节）
    - size_human: 人类可读大小（如 1.2 MB）
    - created_at: 文件创建时间（ISO 8601，取自 stat.st_ctime）
    - modified_at: 最后修改时间（ISO 8601，取自 stat.st_mtime）
    - is_rotated: 是否为轮转文件（文件名含日期后缀）

    服务层 /files 调用此端点获取真实文件列表，而非伪造文件名。
    客户端可依据 created_at / modified_at 决定下载哪个日志文件。
    """
    try:
        log_files = _get_kernel_log_files()
        if not log_files:
            return {"files": [], "total": 0, "log_dir": str(_get_kernel_log_dir())}

        log_dir = _get_kernel_log_dir()
        result: List[Dict[str, Any]] = []
        for f in log_files:
            try:
                stat = f.stat()
                rel_path = str(f.relative_to(log_dir)).replace("\\", "/")
                # 按目录推断日志类型
                parent = rel_path.split("/")[0] if "/" in rel_path else "run"
                log_type_map = {
                    "run": "common",
                    "interface": "interface",
                    "performance": "performance",
                }
                log_type = log_type_map.get(parent, "common")
                # 轮转文件判断（loguru 按日期轮转，如 jiuwen.log.2026-07-09）
                is_rotated = ".log." in f.name and not f.name.endswith(".log")

                # 人类可读大小
                size = stat.st_size
                if size >= 1073741824:
                    size_human = f"{size / 1073741824:.1f} GB"
                elif size >= 1048576:
                    size_human = f"{size / 1048576:.1f} MB"
                elif size >= 1024:
                    size_human = f"{size / 1024:.1f} KB"
                else:
                    size_human = f"{size} B"

                result.append({
                    "filename": rel_path,
                    "log_type": log_type,
                    "size_bytes": size,
                    "size_human": size_human,
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "is_rotated": is_rotated,
                })
            except (OSError, ValueError):
                continue

        return {
            "files": result,
            "total": len(result),
            "log_dir": str(log_dir),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing log files: {str(e)}") from e


# ---------------------------------------------------------------------------
# 注册函数 — 由 memory_server.py 调用
# ---------------------------------------------------------------------------

def register_log_center_endpoints(app, *, memory_engine):
    """注册日志中心所有端点到 FastAPI app。

    参数由 memory_server.py 注入：
    - memory_engine: LongTermMemory 单例
    """
    global _memory_engine
    _memory_engine = memory_engine
    app.include_router(router)
