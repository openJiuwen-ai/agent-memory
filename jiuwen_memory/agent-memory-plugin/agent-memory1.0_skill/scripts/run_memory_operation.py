#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-memory1.0_skill — 通过 HTTP API 调用 agent-memory1.0 记忆服务（完整版，与 MemoryRail 功能对等）

用法（通过 call_mcp script_params 传入）：
  {"operation": "search",          "user_id": "XXX", "query": "用户历史处理记录", "top_k": 5}
  {"operation": "get",             "user_id": "XXX", "top_k": 20}
  {"operation": "save",            "user_id": "XXX", "content": "处理结果...", "role": "assistant"}
  {"operation": "update",          "user_id": "XXX", "mem_id": "mem_abc123", "memory": "修正后的内容"}
  {"operation": "delete",          "user_id": "XXX", "mem_id": "mem_abc123"}
  {"operation": "batch_delete",    "user_id": "XXX", "mem_ids": ["mem_a", "mem_b"]}
  {"operation": "update_variables","user_id": "XXX", "variables": {"status": "normal"}}
  {"operation": "delete_variables","user_id": "XXX", "names": ["old_var_name"]}
  {"operation": "trace",           "message_id": "msg_abc123"}
  {"operation": "flush",           "user_id": "XXX"}
  {"operation": "status"}

环境变量：
  MEM1_BASE_URL                 — agent-memory1.0 服务地址，默认 <YOUR_MEMORY_BASE_URL>
  MEM1_API_KEY                  — API 认证密钥，默认 <YOUR_MEMORY_API_KEY>
  MEM1_TIMEOUT                  — 请求超时秒数，默认 30
  MEM1_STATE_DIR                — 熔断器/缓冲区状态文件目录，默认 /tmp/mem1_skill_state
  MEM1_CIRCUIT_BREAKER_THRESHOLD — 熔断器连续失败阈值，默认 5
  MEM1_CIRCUIT_BREAKER_COOLDOWN  — 熔断器冷却秒数，默认 120
  MEM1_BUFFER_CHARS_THRESHOLD    — 缓冲区字符数阈值（超过则自动 flush），默认 20000
  MEM1_DEFAULT_SCOPE_ID          — 默认 scope_id，默认 edp_agent

独立测试：
  python run_memory_operation.py '{"operation":"search","user_id":"<YOUR_USER_ID>","query":"业务关键词","top_k":5}'
"""
import json
import logging
import os
import sys
import time
import traceback
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── emit_result 专用 logger：输出到 stdout 供 Agent 通过 call_mcp 读取 ──
# 这不是常规日志，而是结构化 JSON 通信通道，因此不添加时间戳/级别等元信息
_emit_log = logging.getLogger("mem1_skill_emit")
_emit_log.propagate = False
if not _emit_log.handlers:
    _emit_handler = logging.StreamHandler(sys.stdout)
    _emit_handler.setFormatter(logging.Formatter("%(message)s"))
    _emit_log.addHandler(_emit_handler)
    _emit_log.setLevel(logging.INFO)


# ── 配置 ──────────────────────────────────────────────────────────
MEM1_BASE_URL = os.environ.get("MEM1_BASE_URL", "<YOUR_MEMORY_BASE_URL>")
MEM1_API_KEY = os.environ.get("MEM1_API_KEY", "<YOUR_MEMORY_API_KEY>")
MEM1_TIMEOUT = int(os.environ.get("MEM1_TIMEOUT", "30"))
MEM1_STATE_DIR = os.environ.get("MEM1_STATE_DIR", "/tmp/mem1_skill_state")
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("MEM1_CIRCUIT_BREAKER_THRESHOLD", "5"))
CIRCUIT_BREAKER_COOLDOWN = int(os.environ.get("MEM1_CIRCUIT_BREAKER_COOLDOWN", "120"))
BUFFER_CHARS_THRESHOLD = int(os.environ.get("MEM1_BUFFER_CHARS_THRESHOLD", "20000"))
MSG_CONTENT_MAX_CHARS = 3900
DEFAULT_SCOPE_ID = os.environ.get("MEM1_DEFAULT_SCOPE_ID", "edp_agent")


# ── 输出 ──────────────────────────────────────────────────────────
def emit_result(data: dict) -> None:
    """输出 JSON 结果到 stdout（Agent 通过 call_mcp 读取）。"""
    _emit_log.info(json.dumps(data, ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════════
# 熔断器 (Circuit Breaker) — 文件级持久化，跨进程共享
# ══════════════════════════════════════════════════════════════════

def _cb_state_path() -> str:
    return os.path.join(MEM1_STATE_DIR, "circuit_breaker.json")


def _ensure_state_dir() -> None:
    os.makedirs(MEM1_STATE_DIR, exist_ok=True)


def _read_cb_state() -> dict:
    """读取熔断器状态文件。"""
    path = _cb_state_path()
    if not os.path.exists(path):
        return {"failure_count": 0, "breaker_open_until": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"failure_count": 0, "breaker_open_until": 0.0}


def _write_cb_state(state: dict) -> None:
    """写入熔断器状态文件。"""
    _ensure_state_dir()
    try:
        with open(_cb_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except IOError:
        pass  # 写入失败不影响主流程（fail-open）


def _is_breaker_open() -> bool:
    """检查熔断器是否打开。冷却时间到后自动复位。"""
    state = _read_cb_state()
    breaker_open_until = state.get("breaker_open_until", 0.0)
    if breaker_open_until > 0 and time.time() < breaker_open_until:
        return True
    # 冷却时间已过，自动复位
    if breaker_open_until > 0:
        _write_cb_state({"failure_count": 0, "breaker_open_until": 0.0})
    return False


def _record_failure() -> None:
    """记录一次失败。连续失败达到阈值时打开熔断器。"""
    state = _read_cb_state()
    state["failure_count"] = state.get("failure_count", 0) + 1
    if state["failure_count"] >= CIRCUIT_BREAKER_THRESHOLD:
        state["breaker_open_until"] = time.time() + CIRCUIT_BREAKER_COOLDOWN
    _write_cb_state(state)


def _record_success() -> None:
    """记录一次成功，重置熔断器。"""
    _write_cb_state({"failure_count": 0, "breaker_open_until": 0.0})


# ══════════════════════════════════════════════════════════════════
# 消息缓冲区 (Batch Write Buffer) — 文件级持久化
# ══════════════════════════════════════════════════════════════════

def _buffer_path(user_id: str) -> str:
    safe_uid = user_id.replace("/", "_").replace("\\", "_")
    return os.path.join(MEM1_STATE_DIR, f"buffer_{safe_uid}.jsonl")


def _buffer_append(user_id: str, messages: list) -> int:
    """追加消息到缓冲区文件，返回当前缓冲区总字符数。"""
    _ensure_state_dir()
    path = _buffer_path(user_id)
    total_chars = 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            for msg in messages:
                line = json.dumps(msg, ensure_ascii=False) + "\n"
                f.write(line)
                total_chars += len(line)
    except IOError:
        pass
    # 计算总字符数
    try:
        with open(path, "r", encoding="utf-8") as f:
            total_chars = len(f.read())
    except IOError:
        total_chars = 0
    return total_chars


def _buffer_read_all(user_id: str) -> list:
    """读取缓冲区所有消息。"""
    path = _buffer_path(user_id)
    messages = []
    if not os.path.exists(path):
        return messages
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except IOError:
        pass
    return messages


def _buffer_clear(user_id: str) -> None:
    """清空缓冲区。"""
    path = _buffer_path(user_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except IOError:
        pass


def _buffer_total_chars(user_id: str) -> int:
    """获取缓冲区总字符数。"""
    path = _buffer_path(user_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return len(f.read())
    except IOError:
        pass
    return 0


# ══════════════════════════════════════════════════════════════════
# HTTP 调用
# ══════════════════════════════════════════════════════════════════

def _call_api(endpoint: str, body: dict, timeout: int = None) -> tuple:
    """调用 agent-memory1.0 API，返回 (success, data_or_error)。

    熔断器打开时直接返回失败，不发起 HTTP 请求（fail-open）。
    """
    if _is_breaker_open():
        return False, "熔断器已打开，跳过 HTTP 调用"

    if timeout is None:
        timeout = MEM1_TIMEOUT
    url = f"{MEM1_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {MEM1_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        resp = urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode("utf-8"))
        _record_success()
        return True, result
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        _record_failure()
        return False, f"HTTP {e.code}: {detail[:500]}"
    except URLError as e:
        _record_failure()
        return False, f"连接失败: {e.reason}"
    except Exception as e:
        _record_failure()
        return False, f"{type(e).__name__}: {e}"


def _truncate_content(content: str, max_chars: int = None) -> str:
    """截断过长的消息内容。"""
    if max_chars is None:
        max_chars = MSG_CONTENT_MAX_CHARS
    if len(content) > max_chars:
        return content[:max_chars] + f"…[内容已截断，原始长度 {len(content)} 字符]"
    return content


# ══════════════════════════════════════════════════════════════════
# 操作：搜索 (search)
# ══════════════════════════════════════════════════════════════════

def _search_memory(user_id: str, query: str, top_k: int, threshold: float, scope_id: str = "") -> dict:
    """
    四路召回（与 MemoryRail _http_search_mem1 完全对等）：
    1. get_variables          — 用户画像变量（risk_preference 等）
    2. search_memory          — 向量语义搜索
    3. search_user_history_summary — 对话摘要搜索
    4. get_user_mem_by_page   — 页面检索（兜底）
    """
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    results = []
    search_details = []
    variables = {}

    # ── 第 1 路：用户画像变量 ──
    try:
        ok, data = _call_api("get_variables/", {
            "user_id": user_id,
            "scope_id": scope_id,
        })
        if ok and isinstance(data, dict):
            variables = data
            search_details.append(f"画像变量 {len(variables)} 项")
        else:
            search_details.append(f"画像变量获取失败: {data}")
    except Exception as e:
        search_details.append(f"画像变量异常: {e}")

    # ── 第 2 路：向量搜索 ──
    vector_count = 0
    try:
        ok, data = _call_api("search_memory/", {
            "query": query,
            "num": top_k,
            "user_id": user_id,
            "scope_id": scope_id,
            "threshold": threshold,
        })
        if ok and isinstance(data, dict):
            items = data.get("results", [])
            for item in items:
                results.append({
                    "content": item.get("content", ""),
                    "type": item.get("type", "unknown"),
                    "score": item.get("score", 0),
                    "source": "vector_search",
                    "mem_id": item.get("mem_id", item.get("id", "")),
                    "source_id": item.get("source_id", item.get("message_id", "")),
                })
            vector_count = len(items)
            search_details.append(f"向量搜索 {vector_count} 条")
        else:
            search_details.append(f"向量搜索失败: {data}")
    except Exception as e:
        search_details.append(f"向量搜索异常: {e}")

    # ── 第 3 路：对话摘要搜索 ──
    summary_count = 0
    try:
        ok, data = _call_api("search_user_history_summary/", {
            "query": query,
            "num": 5,
            "user_id": user_id,
            "scope_id": scope_id,
            "threshold": threshold,
        })
        if ok and isinstance(data, dict):
            existing_contents = {r["content"] for r in results}
            items = data.get("results", [])
            for item in items:
                content = item.get("content", "")
                if content and content not in existing_contents:
                    results.append({
                        "content": content,
                        "type": "summary",
                        "score": item.get("score", 0),
                        "source": "summary_search",
                        "mem_id": item.get("mem_id", item.get("id", "")),
                        "source_id": item.get("source_id", item.get("message_id", "")),
                    })
                    existing_contents.add(content)
            summary_count = len(items)
            search_details.append(f"摘要搜索 {summary_count} 条")
        else:
            search_details.append(f"摘要搜索失败: {data}")
    except Exception as e:
        search_details.append(f"摘要搜索异常: {e}")

    # ── 第 4 路：页面检索（兜底，当向量搜索返回空时）──
    page_count = 0
    if not results:
        try:
            ok, data = _call_api("get_user_mem_by_page/", {
                "user_id": user_id,
                "scope_id": scope_id,
                "page_num": 1,
                "page_size": max(top_k, 10),
            })
            if ok and isinstance(data, dict):
                items = data.get("results", [])
                existing_contents = {r["content"] for r in results}
                for item in items:
                    content = item.get("content", "")
                    if content not in existing_contents:
                        results.append({
                            "content": content,
                            "type": item.get("type", "summary"),
                            "score": 0.5,
                            "source": "page_retrieval",
                            "mem_id": item.get("mem_id", item.get("id", "")),
                            "source_id": item.get("source_id", item.get("message_id", "")),
                        })
                        existing_contents.add(content)
                page_count = len(items)
                search_details.append(f"页面检索 {page_count} 条")
            else:
                search_details.append(f"页面检索失败: {data}")
        except Exception as e:
            search_details.append(f"页面检索异常: {e}")

    # 截断到 top_k
    results = results[:top_k]

    return {
        "success": len(results) > 0,
        "operation": "search",
        "user_id": user_id,
        "scope_id": scope_id,
        "query": query,
        "total_results": len(results),
        "variables": variables,
        "results": results,
        "search_summary": f"共召回 {len(results)} 条记忆: " + " + ".join(search_details),
        "breaker_open": _is_breaker_open(),
    }


# ══════════════════════════════════════════════════════════════════
# 操作：列出 (get)
# ══════════════════════════════════════════════════════════════════

def _get_memory(user_id: str, top_k: int, scope_id: str = "") -> dict:
    """列出记忆（审计/调试用，返回全局 total 确保完整性）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    ok, data = _call_api("get_user_mem_by_page_with_total/", {
        "user_id": user_id,
        "scope_id": scope_id,
        "page_num": 1,
        "page_size": top_k,
    })

    if ok and isinstance(data, dict):
        results = data.get("results", [])
        return {
            "success": True,
            "operation": "get",
            "user_id": user_id,
            "total_results": len(results),
            "total_global": data.get("total", len(results)),
            "results": results,
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "get",
            "user_id": user_id,
            "error": str(data),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：保存 (save)
# ══════════════════════════════════════════════════════════════════

def _save_memory(
    user_id: str,
    content: str = "",
    role: str = "assistant",
    messages: list = None,
    options: dict = None,
) -> dict:
    """写入记忆到 agent-memory1.0。

    支持两种写入模式：
    - 直接写入（buffer=False，默认）：立即调用 add_messages API
    - 缓冲写入（buffer=True）：追加到缓冲区文件，满足条件时自动 flush

    options 可选字段：
    - session_id, scope_id, buffer
    - enable_long_term_mem, enable_semantic_memory,
      enable_episodic_memory, enable_summary_memory,
      enable_user_profile
    """
    if options is None:
        options = {}
    session_id = options.get("session_id", "")
    scope_id = options.get("scope_id", "")
    buffer = options.get("buffer", False)
    enable_long_term_mem = options.get("enable_long_term_mem", True)
    enable_semantic_memory = options.get("enable_semantic_memory", True)
    enable_episodic_memory = options.get("enable_episodic_memory", True)
    enable_summary_memory = options.get("enable_summary_memory", True)
    enable_user_profile = options.get("enable_user_profile", True)
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    # 构建消息列表
    msg_list = []
    if messages:
        for m in messages:
            c = _truncate_content(m.get("content", "") or "")
            if c:
                msg_list.append({"role": m.get("role", "user"), "content": c})
    elif content:
        c = _truncate_content(content)
        if c:
            msg_list.append({"role": role, "content": c})

    if not msg_list:
        return {
            "success": False,
            "operation": "save",
            "user_id": user_id,
            "scope_id": scope_id,
            "error": "没有可写入的消息内容",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    # ── 缓冲模式 ──
    if buffer:
        current_chars = _buffer_append(user_id, msg_list)
        # 检查是否超过阈值，自动 flush
        if current_chars >= BUFFER_CHARS_THRESHOLD:
            enable_flags = {
                "enable_long_term_mem": enable_long_term_mem,
                "enable_semantic_memory": enable_semantic_memory,
                "enable_episodic_memory": enable_episodic_memory,
                "enable_summary_memory": enable_summary_memory,
                "enable_user_profile": enable_user_profile,
            }
            return _flush_buffer(user_id, session_id, scope_id, enable_flags)
        return {
            "success": True,
            "operation": "save",
            "user_id": user_id,
            "scope_id": scope_id,
            "mode": "buffered",
            "buffered_messages": len(msg_list),
            "buffer_total_chars": current_chars,
            "buffer_threshold": BUFFER_CHARS_THRESHOLD,
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    # ── 直接写入模式 ──
    body = {
        "user_id": user_id,
        "scope_id": scope_id,
        "messages": msg_list,
        "enable_long_term_mem": enable_long_term_mem,
        "enable_semantic_memory": enable_semantic_memory,
        "enable_episodic_memory": enable_episodic_memory,
        "enable_summary_memory": enable_summary_memory,
        "enable_user_profile": enable_user_profile,
    }
    if session_id:
        body["session_id"] = session_id

    ok, data = _call_api("add_messages/", body)

    if ok:
        return {
            "success": True,
            "operation": "save",
            "user_id": user_id,
            "scope_id": scope_id,
            "mode": "direct",
            "message_count": len(msg_list),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "save",
            "user_id": user_id,
            "scope_id": scope_id,
            "mode": "direct",
            "error": str(data),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：刷新缓冲区 (flush)
# ══════════════════════════════════════════════════════════════════

def _flush_buffer(
    user_id: str,
    session_id: str = "",
    scope_id: str = "",
    enable_flags: dict = None,
) -> dict:
    """将缓冲区中的所有消息批量写入 agent-memory1.0。

    enable_flags 可选字段：enable_long_term_mem, enable_semantic_memory,
    enable_episodic_memory, enable_summary_memory, enable_user_profile
    """
    if enable_flags is None:
        enable_flags = {}
    enable_long_term_mem = enable_flags.get("enable_long_term_mem", True)
    enable_semantic_memory = enable_flags.get("enable_semantic_memory", True)
    enable_episodic_memory = enable_flags.get("enable_episodic_memory", True)
    enable_summary_memory = enable_flags.get("enable_summary_memory", True)
    enable_user_profile = enable_flags.get("enable_user_profile", True)
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    messages = _buffer_read_all(user_id)
    if not messages:
        return {
            "success": True,
            "operation": "flush",
            "user_id": user_id,
            "message": "缓冲区为空，无需刷新",
            "flushed_count": 0,
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    # 截断每条消息
    truncated = []
    for m in messages:
        c = _truncate_content(m.get("content", "") or "")
        if c:
            truncated.append({"role": m.get("role", "user"), "content": c})

    if not truncated:
        _buffer_clear(user_id)
        return {
            "success": True,
            "operation": "flush",
            "user_id": user_id,
            "message": "截断后无有效内容",
            "flushed_count": 0,
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    body = {
        "user_id": user_id,
        "scope_id": scope_id,
        "messages": truncated,
        "enable_long_term_mem": enable_long_term_mem,
        "enable_semantic_memory": enable_semantic_memory,
        "enable_episodic_memory": enable_episodic_memory,
        "enable_summary_memory": enable_summary_memory,
        "enable_user_profile": enable_user_profile,
    }
    if session_id:
        body["session_id"] = session_id

    ok, data = _call_api("add_messages/", body)

    if ok:
        _buffer_clear(user_id)
        return {
            "success": True,
            "operation": "flush",
            "user_id": user_id,
            "scope_id": scope_id,
            "flushed_count": len(truncated),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "flush",
            "user_id": user_id,
            "error": str(data),
            "buffered_count": len(truncated),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：更新记忆 (update)
# ══════════════════════════════════════════════════════════════════

def _update_memory(mem_id: str, memory: str, user_id: str, scope_id: str = "") -> dict:
    """更新单条记忆内容（调用 /update_mem_by_id/）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    if not mem_id:
        return {
            "success": False,
            "operation": "update",
            "user_id": user_id,
            "error": "mem_id is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    if not memory:
        return {
            "success": False,
            "operation": "update",
            "user_id": user_id,
            "mem_id": mem_id,
            "error": "memory content is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    ok, data = _call_api("update_mem_by_id/", {
        "mem_id": mem_id,
        "memory": _truncate_content(memory),
        "user_id": user_id,
        "scope_id": scope_id,
    })

    return {
        "success": ok,
        "operation": "update",
        "user_id": user_id,
        "scope_id": scope_id,
        "mem_id": mem_id,
        "message": "Memory updated successfully" if ok else str(data),
        "results": [],
        "breaker_open": _is_breaker_open(),
    }


# ══════════════════════════════════════════════════════════════════
# 操作：删除单条记忆 (delete)
# ══════════════════════════════════════════════════════════════════

def _delete_memory(mem_id: str, user_id: str, scope_id: str = "") -> dict:
    """删除单条记忆（调用 /delete_mem_by_id/）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    if not mem_id:
        return {
            "success": False,
            "operation": "delete",
            "user_id": user_id,
            "error": "mem_id is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    ok, data = _call_api("delete_mem_by_id/", {
        "mem_id": mem_id,
        "user_id": user_id,
        "scope_id": scope_id,
    })

    return {
        "success": ok,
        "operation": "delete",
        "user_id": user_id,
        "scope_id": scope_id,
        "mem_id": mem_id,
        "message": "Memory deleted successfully" if ok else str(data),
        "results": [],
        "breaker_open": _is_breaker_open(),
    }


# ══════════════════════════════════════════════════════════════════
# 操作：批量删除记忆 (batch_delete)
# ══════════════════════════════════════════════════════════════════

def _batch_delete_memory(mem_ids: list, user_id: str, scope_id: str = "") -> dict:
    """批量删除多条记忆（调用 /batch_delete_mem/）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    if not mem_ids:
        return {
            "success": False,
            "operation": "batch_delete",
            "user_id": user_id,
            "error": "mem_ids is required and must not be empty",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    ok, data = _call_api("batch_delete_mem/", {
        "mem_ids": mem_ids,
        "user_id": user_id,
        "scope_id": scope_id,
    })

    if ok and isinstance(data, dict):
        return {
            "success": True,
            "operation": "batch_delete",
            "user_id": user_id,
            "scope_id": scope_id,
            "deleted": data.get("deleted", 0),
            "failed": data.get("failed", 0),
            "errors": data.get("errors", []),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "batch_delete",
            "user_id": user_id,
            "scope_id": scope_id,
            "error": str(data),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：更新画像变量 (update_variables)
# ══════════════════════════════════════════════════════════════════

def _update_variables(variables: dict, user_id: str, scope_id: str = "") -> dict:
    """更新用户画像变量（调用 /update_variables/）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    if not variables:
        return {
            "success": False,
            "operation": "update_variables",
            "user_id": user_id,
            "error": "variables dict is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    ok, data = _call_api("update_variables/", {
        "variables": variables,
        "user_id": user_id,
        "scope_id": scope_id,
    })

    return {
        "success": ok,
        "operation": "update_variables",
        "user_id": user_id,
        "scope_id": scope_id,
        "updated_count": len(variables),
        "message": "Variables updated successfully" if ok else str(data),
        "results": [],
        "breaker_open": _is_breaker_open(),
    }


# ══════════════════════════════════════════════════════════════════
# 操作：删除画像变量 (delete_variables)
# ══════════════════════════════════════════════════════════════════

def _delete_variables(names: list, user_id: str, scope_id: str = "") -> dict:
    """删除用户画像变量（调用 /delete_variables/）。"""
    if not scope_id:
        scope_id = DEFAULT_SCOPE_ID
    if not names:
        return {
            "success": False,
            "operation": "delete_variables",
            "user_id": user_id,
            "error": "names list is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    ok, data = _call_api("delete_variables/", {
        "names": names,
        "user_id": user_id,
        "scope_id": scope_id,
    })

    if ok and isinstance(data, dict):
        return {
            "success": True,
            "operation": "delete_variables",
            "user_id": user_id,
            "scope_id": scope_id,
            "deleted": data.get("deleted", 0),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "delete_variables",
            "user_id": user_id,
            "error": str(data),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：消息溯源 (trace)
# ══════════════════════════════════════════════════════════════════

def _trace_message(message_id: str, user_id: str = "") -> dict:
    """追溯记忆的原始来源消息（调用 /get_message_by_id/）。"""
    if not message_id:
        return {
            "success": False,
            "operation": "trace",
            "error": "message_id is required",
            "results": [],
            "breaker_open": _is_breaker_open(),
        }

    payload = {"message_id": message_id}
    if user_id:
        payload["user_id"] = user_id

    ok, data = _call_api("get_message_by_id/", payload)

    if ok and isinstance(data, dict):
        return {
            "success": True,
            "operation": "trace",
            "message_id": message_id,
            "message": data,
            "results": [],
            "breaker_open": _is_breaker_open(),
        }
    else:
        return {
            "success": False,
            "operation": "trace",
            "message_id": message_id,
            "error": str(data),
            "results": [],
            "breaker_open": _is_breaker_open(),
        }


# ══════════════════════════════════════════════════════════════════
# 操作：状态 (status)
# ══════════════════════════════════════════════════════════════════

def _get_status() -> dict:
    """获取熔断器状态和缓冲区概况。"""
    cb_state = _read_cb_state()
    breaker_open = _is_breaker_open()

    # 扫描缓冲区文件
    buffers = {}
    _ensure_state_dir()
    try:
        for fname in os.listdir(MEM1_STATE_DIR):
            if fname.startswith("buffer_") and fname.endswith(".jsonl"):
                uid = fname[len("buffer_"):-len(".jsonl")]
                fpath = os.path.join(MEM1_STATE_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        line_count = sum(1 for _ in f)
                    with open(fpath, "r", encoding="utf-8") as f:
                        char_count = len(f.read())
                except IOError:
                    line_count = 0
                    char_count = 0
                buffers[uid] = {"message_count": line_count, "total_chars": char_count}
    except IOError:
        pass

    return {
        "success": True,
        "operation": "status",
        "circuit_breaker": {
            "open": breaker_open,
            "failure_count": cb_state.get("failure_count", 0),
            "threshold": CIRCUIT_BREAKER_THRESHOLD,
            "cooldown_seconds": CIRCUIT_BREAKER_COOLDOWN,
            "breaker_open_until": cb_state.get("breaker_open_until", 0.0),
        },
        "config": {
            "base_url": MEM1_BASE_URL,
            "timeout": MEM1_TIMEOUT,
            "buffer_chars_threshold": BUFFER_CHARS_THRESHOLD,
            "state_dir": MEM1_STATE_DIR,
        },
        "buffers": buffers,
        "results": [],
    }


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

def main():
    # 解析参数：优先从命令行参数，其次从环境变量
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            raw = os.environ.get("MCP_SCRIPT_PARAMS", "")
            if raw:
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    emit_result({
                        "success": False,
                        "error": f"无法解析参数: {sys.argv[1][:200]}",
                        "results": [],
                    })
                    return
    else:
        raw = os.environ.get("MCP_SCRIPT_PARAMS", "")
        if raw:
            try:
                params = json.loads(raw)
            except json.JSONDecodeError:
                pass

    if not params:
        emit_result({
            "success": False,
            "error": "未提供参数。用法: python run_memory_operation.py '{\"operation\":\"search\",\"user_id\":\"...\"}'",
            "results": [],
        })
        return

    operation = params.get("operation", "search")
    user_id = str(params.get("user_id", "")).strip()
    scope_id = str(params.get("scope_id", DEFAULT_SCOPE_ID)).strip()

    # status 操作不需要 user_id
    if operation == "status":
        try:
            emit_result(_get_status())
        except Exception as e:
            emit_result({"success": False, "operation": "status", "error": f"{type(e).__name__}: {e}", "results": []})
        return

    if not user_id:
        emit_result({
            "success": False,
            "operation": operation,
            "error": "user_id is required",
            "results": [],
        })
        return

    try:
        if operation == "search":
            query = str(params.get("query", "业务关键词")).strip()
            top_k = int(params.get("top_k", 5))
            threshold = float(params.get("threshold", 0.0))
            result = _search_memory(user_id, query, top_k, threshold, scope_id)

        elif operation == "get":
            top_k = int(params.get("top_k", 20))
            result = _get_memory(user_id, top_k, scope_id)

        elif operation == "save":
            content = str(params.get("content", ""))
            role = str(params.get("role", "assistant"))
            messages = params.get("messages", None)
            save_options = {
                "session_id": str(params.get("session_id", "")),
                "scope_id": scope_id,
                "buffer": bool(params.get("buffer", False)),
                "enable_long_term_mem": bool(params.get("enable_long_term_mem", True)),
                "enable_semantic_memory": bool(params.get("enable_semantic_memory", True)),
                "enable_episodic_memory": bool(params.get("enable_episodic_memory", True)),
                "enable_summary_memory": bool(params.get("enable_summary_memory", True)),
                "enable_user_profile": bool(params.get("enable_user_profile", True)),
            }
            result = _save_memory(
                user_id, content, role, messages, save_options,
            )

        elif operation == "update":
            mem_id = str(params.get("mem_id", "")).strip()
            memory = str(params.get("memory", ""))
            result = _update_memory(mem_id, memory, user_id, scope_id)

        elif operation == "delete":
            mem_id = str(params.get("mem_id", "")).strip()
            result = _delete_memory(mem_id, user_id, scope_id)

        elif operation == "batch_delete":
            mem_ids = params.get("mem_ids", [])
            if isinstance(mem_ids, str):
                try:
                    mem_ids = json.loads(mem_ids)
                except json.JSONDecodeError:
                    mem_ids = [mem_ids]
            result = _batch_delete_memory(mem_ids, user_id, scope_id)

        elif operation == "update_variables":
            variables = params.get("variables", {})
            if isinstance(variables, str):
                try:
                    variables = json.loads(variables)
                except json.JSONDecodeError:
                    variables = {}
            result = _update_variables(variables, user_id, scope_id)

        elif operation == "delete_variables":
            names = params.get("names", [])
            if isinstance(names, str):
                try:
                    names = json.loads(names)
                except json.JSONDecodeError:
                    names = [names]
            result = _delete_variables(names, user_id, scope_id)

        elif operation == "trace":
            message_id = str(params.get("message_id", "")).strip()
            result = _trace_message(message_id, user_id)

        elif operation == "flush":
            session_id = str(params.get("session_id", ""))
            result = _flush_buffer(user_id, session_id, scope_id)

        else:
            supported = (
                "search / get / save / update / delete / batch_delete / "
                "update_variables / delete_variables / trace / flush / status"
            )
            result = {
                "success": False,
                "operation": operation,
                "error": f"不支持的操作: {operation}，支持: {supported}",
                "results": [],
            }

    except Exception as e:
        result = {
            "success": False,
            "operation": operation,
            "user_id": user_id,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
            "results": [],
        }

    emit_result(result)


if __name__ == "__main__":
    main()