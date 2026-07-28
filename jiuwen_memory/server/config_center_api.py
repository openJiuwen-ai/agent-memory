# -*- coding: UTF-8 -*-
"""配置中心 API 模块 — 抽离自 memory_server.py

职责（设计文档 §5.3.6）：
  - Scope 配置热加载（set/get/delete_scope_config）
  - Dreaming 跨会话整合（start/stop_dreaming, dreaming/status）
  - 内核配置管理（admin/config GET+PUT, reload-config, restart）

由 memory_server.py 调用 register_config_center_endpoints(app, ...) 注册到 FastAPI app。
共享的 env 工具函数（_env_value/_bool_env/_typed_env_value/_resolve_env_path）和
引擎生命周期函数（_reload_memory_engine/shutdown_event）由 memory_server.py 注入，
避免循环导入。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from dotenv import set_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType
from jiuwen_memory.memory_core.config.config import DreamingConfig, MemoryScopeConfig

# ---------------------------------------------------------------------------
# 配置中心常量（设计文档 §5.3.6）
# ---------------------------------------------------------------------------

ARCHITECTURE_PARAMS = {
    "IP",
    "PORT",
    "MEMORY_DATA_DIR",
    "KV_STORE_TYPE",
    "DB_STORE_TYPE",
    "VECTOR_STORE_TYPE",
    "CRYPTO_KEY",
    "INDEX_BACKEND",
}

EDITABLE_ENV_PARAMS = {
    "MODEL_PROVIDER",
    "API_BASE",
    "API_KEY",
    "MODEL_NAME",
    "MODEL_SSL_VERIFY",
    "EMBED_MODEL_NAME",
    "EMBED_API_BASE",
    "EMBED_API_KEY",
    "MEMORY_API_KEY",
    "DB_URL",
    "KV_SHELVE_PATH",
    "FILE_MEMORY_DATA_DIR",
    "VECTOR_CHROMA_PERSIST_DIR",
    "VECTOR_MILVUS_URI",
    "VECTOR_MILVUS_TOKEN",
    "VECTOR_MILVUS_DATABASE",
    "VECTOR_ES_HOSTS",
    "VECTOR_ES_INDEX_PREFIX",
    "VECTOR_ES_USERNAME",
    "VECTOR_ES_PASSWORD",
    "VECTOR_ES_API_KEY",
    "VECTOR_ES_VERIFY_CERTS",
    "VECTOR_ES_CA_CERTS",
    "VECTOR_ES_CLIENT_CERT",
    "VECTOR_ES_CLIENT_KEY",
    "VECTOR_GAUSS_HOST",
    "VECTOR_GAUSS_PORT",
    "VECTOR_GAUSS_DATABASE",
    "VECTOR_GAUSS_USER",
    "VECTOR_GAUSS_PASSWORD",
    "RERANK_API_BASE",
    "RERANK_API_KEY",
    "RERANK_MODEL_NAME",
    "RERANK_THRESHOLD",
    "RERANK_POOL_FACTOR",
    "MEMORY_ENABLE_MIDDLE_MEMORY",
    "MEMORY_MIDDLE_CHECK_INTERVAL",
    "MEMORY_ENABLE_FORGETTING",
    "MEMORY_FORGET_THRESHOLD",
    "MEMORY_FORGET_MAX_EVICT",
    "MEMORY_FORGET_MIN_RETENTION_DAYS",
    "DREAMING_ENABLED",
    "DREAMING_INTERVAL_SECONDS",
}

SENSITIVE_KEYS = {
    "API_KEY",
    "EMBED_API_KEY",
    "MEMORY_API_KEY",
    "RERANK_API_KEY",
    "VECTOR_MILVUS_TOKEN",
    "VECTOR_ES_PASSWORD",
    "VECTOR_ES_API_KEY",
    "VECTOR_GAUSS_PASSWORD",
    "CRYPTO_KEY",
}


def _schema_entry(env, editable, category, danger):
    """Build a single CONFIG_SCHEMA entry dict (keeps lines under 120 chars)."""
    return {
        "env": env,
        "editable": editable,
        "category": category,
        "danger": danger,
    }


CONFIG_SCHEMA = {
    "runtime": {
        "ip": _schema_entry("IP", False, "architecture", "safe"),
        "port": _schema_entry("PORT", False, "architecture", "safe"),
        "memory_api_key": _schema_entry("MEMORY_API_KEY", True, "connection", "warning"),
        "memory_data_dir": _schema_entry("MEMORY_DATA_DIR", False, "architecture", "critical"),
    },
    "storage": {
        "kv_store_type": _schema_entry("KV_STORE_TYPE", False, "architecture", "critical"),
        "db_store_type": _schema_entry("DB_STORE_TYPE", False, "architecture", "critical"),
        "vector_store_type": _schema_entry("VECTOR_STORE_TYPE", False, "architecture", "critical"),
        "db_url": _schema_entry("DB_URL", True, "connection", "critical"),
        "kv_shelve_path": _schema_entry("KV_SHELVE_PATH", True, "connection", "safe"),
        "index_backend": _schema_entry("INDEX_BACKEND", False, "architecture", "critical"),
        "file_memory_data_dir": _schema_entry("FILE_MEMORY_DATA_DIR", True, "connection", "warning"),
    },
    "vector_engine": {
        "chroma_persist_dir": _schema_entry("VECTOR_CHROMA_PERSIST_DIR", True, "connection", "safe"),
        "milvus_uri": _schema_entry("VECTOR_MILVUS_URI", True, "connection", "warning"),
        "milvus_token": _schema_entry("VECTOR_MILVUS_TOKEN", True, "connection", "warning"),
        "milvus_database": _schema_entry("VECTOR_MILVUS_DATABASE", True, "connection", "safe"),
        "es_hosts": _schema_entry("VECTOR_ES_HOSTS", True, "connection", "warning"),
        "es_index_prefix": _schema_entry("VECTOR_ES_INDEX_PREFIX", True, "connection", "safe"),
        "es_username": _schema_entry("VECTOR_ES_USERNAME", True, "connection", "warning"),
        "es_password": _schema_entry("VECTOR_ES_PASSWORD", True, "connection", "warning"),
        "es_api_key": _schema_entry("VECTOR_ES_API_KEY", True, "connection", "warning"),
        "es_verify_certs": _schema_entry("VECTOR_ES_VERIFY_CERTS", True, "connection", "safe"),
        "es_ca_certs": _schema_entry("VECTOR_ES_CA_CERTS", True, "connection", "warning"),
        "es_client_cert": _schema_entry("VECTOR_ES_CLIENT_CERT", True, "connection", "warning"),
        "es_client_key": _schema_entry("VECTOR_ES_CLIENT_KEY", True, "connection", "warning"),
        "gauss_host": _schema_entry("VECTOR_GAUSS_HOST", True, "connection", "warning"),
        "gauss_port": _schema_entry("VECTOR_GAUSS_PORT", True, "connection", "safe"),
        "gauss_database": _schema_entry("VECTOR_GAUSS_DATABASE", True, "connection", "safe"),
        "gauss_user": _schema_entry("VECTOR_GAUSS_USER", True, "connection", "warning"),
        "gauss_password": _schema_entry("VECTOR_GAUSS_PASSWORD", True, "connection", "warning"),
    },
    "engine": {
        "model_provider": _schema_entry("MODEL_PROVIDER", True, "connection", "warning"),
        "api_base": _schema_entry("API_BASE", True, "connection", "safe"),
        "api_key": _schema_entry("API_KEY", True, "connection", "safe"),
        "model_name": _schema_entry("MODEL_NAME", True, "connection", "warning"),
        "model_ssl_verify": _schema_entry("MODEL_SSL_VERIFY", True, "connection", "safe"),
        "embed_model_name": _schema_entry("EMBED_MODEL_NAME", True, "connection", "critical"),
        "embed_api_base": _schema_entry("EMBED_API_BASE", True, "connection", "critical"),
        "embed_api_key": _schema_entry("EMBED_API_KEY", True, "connection", "safe"),
        "rerank_api_base": _schema_entry("RERANK_API_BASE", True, "connection", "warning"),
        "rerank_api_key": _schema_entry("RERANK_API_KEY", True, "connection", "warning"),
        "rerank_model_name": _schema_entry("RERANK_MODEL_NAME", True, "connection", "warning"),
        "rerank_threshold": _schema_entry("RERANK_THRESHOLD", True, "connection", "safe"),
        "rerank_pool_factor": _schema_entry("RERANK_POOL_FACTOR", True, "connection", "safe"),
        "crypto_key": _schema_entry("CRYPTO_KEY", False, "architecture", "critical"),
        "enable_middle_memory": _schema_entry("MEMORY_ENABLE_MIDDLE_MEMORY", True, "startup", "warning"),
        "middle_memory_check_interval": _schema_entry("MEMORY_MIDDLE_CHECK_INTERVAL", True, "startup", "safe"),
        "enable_forgetting": _schema_entry("MEMORY_ENABLE_FORGETTING", True, "startup", "warning"),
        "forget_threshold": _schema_entry("MEMORY_FORGET_THRESHOLD", True, "startup", "safe"),
        "forget_max_evict": _schema_entry("MEMORY_FORGET_MAX_EVICT", True, "startup", "safe"),
        "forget_min_retention_days": _schema_entry("MEMORY_FORGET_MIN_RETENTION_DAYS", True, "startup", "safe"),
    },
}

DREAMING_SCHEMA = {
    "enabled": {"env": "DREAMING_ENABLED", "default": "false"},
    "interval_seconds": {"env": "DREAMING_INTERVAL_SECONDS", "default": "14400"},
}

# PUT /admin/config 参数值安全校验规则（设计文档 §5.3.6 第3步：校验 value 合法性）
# 路径类参数：限制安全目录、禁止路径遍历
# URL 类参数：校验格式
# 字符串类参数：长度上限
_MAX_STR_LEN = 4096
_PATH_PARAM_KEYS = {
    "KV_SHELVE_PATH",
    "VECTOR_CHROMA_PERSIST_DIR",
    "FILE_MEMORY_DATA_DIR",
    "VECTOR_ES_CA_CERTS",
    "VECTOR_ES_CLIENT_CERT",
    "VECTOR_ES_CLIENT_KEY",
}
_URL_PARAM_KEYS = {
    "API_BASE",
    "EMBED_API_BASE",
    "RERANK_API_BASE",
    "VECTOR_MILVUS_URI",
    "VECTOR_ES_HOSTS",
}
_URL_PATTERN = re.compile(r"^https?://[^\s]+$")
# 禁止的路径遍历序列
_PATH_TRAVERSAL_PATTERN = re.compile(r"\.\.")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ScopeConfigRequest(BaseModel):
    scope_id: str


class SetScopeConfigRequest(BaseModel):
    scope_id: str
    config: Dict[str, Any]


class AdminConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


class StartDreamingRequest(BaseModel):
    scope_id: str
    user_id: str
    enabled: bool = True
    interval_seconds: float = 14400.0
    min_session_rounds: int = 4
    max_sessions_per_sweep: int = 10
    max_compress_tokens: int = 30000
    max_items_per_session: int = 5


class StopDreamingRequest(BaseModel):
    scope_id: Optional[str] = None
    user_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 模块级依赖引用 — 由 register_config_center_endpoints() 注入
# ---------------------------------------------------------------------------

_memory_engine = None          # LongTermMemory 单例
_env_value = None              # callable (key, default) -> str
_bool_env = None               # callable (key, default) -> bool
_typed_env_value = None        # callable (key, default) -> Any
_resolve_env_path = None       # callable () -> Path
_reload_memory_engine = None   # async callable () -> None
_shutdown_event = None         # async callable () -> None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _config_param_value(env_key: str, default: Any = "") -> Dict[str, Any]:
    if env_key in SENSITIVE_KEYS:
        return {"configured": bool(_env_value(env_key, ""))}
    return {"value": _typed_env_value(env_key, default)}


def _serialize_env_value(key: str, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if key in {
        "MEMORY_ENABLE_MIDDLE_MEMORY",
        "VECTOR_ES_VERIFY_CERTS",
        "MEMORY_ENABLE_FORGETTING",
        "DREAMING_ENABLED",
        "MODEL_SSL_VERIFY",
    }:
        lowered = text.lower()
        if lowered not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError(f"{key} 必须为布尔值")
        return "true" if lowered in {"true", "1", "yes", "on"} else "false"
    if isinstance(value, bool):
        raise ValueError(f"{key} 不接受布尔值")
    if isinstance(value, (int, float)):
        return str(value)
    if key in {
        "PORT",
        "MEMORY_MIDDLE_CHECK_INTERVAL",
        "RERANK_POOL_FACTOR",
        "MEMORY_FORGET_MAX_EVICT",
        "MEMORY_FORGET_MIN_RETENTION_DAYS",
        "DREAMING_INTERVAL_SECONDS",
    }:
        return str(int(text))
    if key in {
        "RERANK_THRESHOLD",
        "MEMORY_FORGET_THRESHOLD",
    }:
        return str(float(text))
    return text


def _admin_config_payload() -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    defaults = {
        "IP": "127.0.0.1",
        "PORT": 8000,
        "MEMORY_DATA_DIR": "",
        "KV_STORE_TYPE": "db",
        "DB_STORE_TYPE": "default",
        "VECTOR_STORE_TYPE": "chroma",
        "DB_URL": "",
        "KV_SHELVE_PATH": "",
        "INDEX_BACKEND": "simple",
        "FILE_MEMORY_DATA_DIR": "",
        "VECTOR_CHROMA_PERSIST_DIR": "",
        "VECTOR_MILVUS_URI": "",
        "VECTOR_MILVUS_TOKEN": "",
        "VECTOR_MILVUS_DATABASE": "default",
        "VECTOR_ES_HOSTS": "",
        "VECTOR_ES_INDEX_PREFIX": "agent_vector",
        "VECTOR_ES_USERNAME": "",
        "VECTOR_ES_PASSWORD": "",
        "VECTOR_ES_API_KEY": "",
        "VECTOR_ES_VERIFY_CERTS": True,
        "VECTOR_ES_CA_CERTS": "",
        "VECTOR_ES_CLIENT_CERT": "",
        "VECTOR_ES_CLIENT_KEY": "",
        "VECTOR_GAUSS_HOST": "localhost",
        "VECTOR_GAUSS_PORT": 5432,
        "VECTOR_GAUSS_DATABASE": "postgres",
        "VECTOR_GAUSS_USER": "postgres",
        "VECTOR_GAUSS_PASSWORD": "",
        "MODEL_PROVIDER": "SiliconFlow",
        "API_BASE": "",
        "API_KEY": "",
        "MODEL_NAME": "default-model",
        "MODEL_SSL_VERIFY": False,
        "EMBED_MODEL_NAME": "",
        "EMBED_API_BASE": "",
        "EMBED_API_KEY": "",
        "RERANK_API_BASE": "",
        "RERANK_API_KEY": "",
        "RERANK_MODEL_NAME": "",
        "RERANK_THRESHOLD": 0.3,
        "RERANK_POOL_FACTOR": 3,
        "CRYPTO_KEY": "",
        "MEMORY_ENABLE_MIDDLE_MEMORY": False,
        "MEMORY_MIDDLE_CHECK_INTERVAL": 50,
        "MEMORY_ENABLE_FORGETTING": False,
        "MEMORY_FORGET_THRESHOLD": 0.15,
        "MEMORY_FORGET_MAX_EVICT": 1000,
        "MEMORY_FORGET_MIN_RETENTION_DAYS": 30,
        "MEMORY_API_KEY": "",
    }
    for section, params in CONFIG_SCHEMA.items():
        payload[section] = {}
        for field_name, meta in params.items():
            item = _config_param_value(meta["env"], defaults.get(meta["env"], ""))
            item["editable"] = meta["editable"]
            item["category"] = meta["category"]
            item["danger"] = meta["danger"]
            payload[section][field_name] = item
    dreaming_interval_env = DREAMING_SCHEMA["interval_seconds"]["env"]
    dreaming_interval_default = DREAMING_SCHEMA["interval_seconds"]["default"]
    payload["dreaming"] = {
        "enabled": _bool_env(DREAMING_SCHEMA["enabled"]["env"], False),
        "interval_seconds": int(_env_value(dreaming_interval_env, dreaming_interval_default)),
    }
    payload["restart_required"] = False
    payload["source"] = str(_resolve_env_path())
    return payload


def _validate_config_value(key: str, value: Any) -> str:
    """校验 PUT /admin/config 参数值安全性，返回序列化后的安全值。

    校验规则（设计文档 §5.3.6）：
    1. 字符串长度上限 _MAX_STR_LEN，防止超长值攻击
    2. 路径类参数禁止路径遍历（..）
    3. URL 类参数校验 http/https 格式
    4. 布尔/数值类参数由 _serialize_env_value 处理
    """
    text = "" if value is None else str(value).strip()
    if len(text) > _MAX_STR_LEN:
        raise ValueError(f"{key} 值长度超限（最大 {_MAX_STR_LEN} 字符）")

    if key in _PATH_PARAM_KEYS:
        if _PATH_TRAVERSAL_PATTERN.search(text):
            raise ValueError(f"{key} 禁止包含路径遍历序列（..）")

    if key in _URL_PARAM_KEYS and text:
        if not _URL_PATTERN.match(text):
            raise ValueError(f"{key} 必须为 http:// 或 https:// 开头的有效 URL")

    return _serialize_env_value(key, value)


# ---------------------------------------------------------------------------
# APIRouter — 配置中心端点
# ---------------------------------------------------------------------------

router = APIRouter()


# ===== Scope 配置热加载端点（配置中心）=====

@router.post("/set_scope_config")
async def set_scope_config_endpoint(request: SetScopeConfigRequest):
    """Hot-load one full MemoryScopeConfig for the given scope."""
    try:
        config = MemoryScopeConfig.model_validate(request.config)
        ok = await _memory_engine.set_scope_config(request.scope_id, config)
        return {"success": bool(ok)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting scope config: {str(e)}") from e


@router.post("/get_scope_config")
async def get_scope_config_endpoint(request: ScopeConfigRequest):
    """Read the current scope override config from kernel KV store."""
    try:
        config = await _memory_engine.get_scope_config(request.scope_id)
        if config is None:
            return {"config": None}
        return {"config": config.model_dump(mode="json", by_alias=True)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting scope config: {str(e)}") from e


@router.post("/delete_scope_config")
async def delete_scope_config_endpoint(request: ScopeConfigRequest):
    """Delete one scope override so the scope falls back to defaults/template."""
    try:
        ok = await _memory_engine.delete_scope_config(request.scope_id)
        return {"success": bool(ok)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting scope config: {str(e)}") from e


# ===== Dreaming 跨会话整合端点（配置中心）=====

@router.post("/start_dreaming")
async def start_dreaming_endpoint(request: StartDreamingRequest):
    """Start cross-session dreaming consolidation for a (scope_id, user_id)."""
    try:
        config = DreamingConfig(
            enabled=request.enabled,
            interval_seconds=request.interval_seconds,
            min_session_rounds=request.min_session_rounds,
            max_sessions_per_sweep=request.max_sessions_per_sweep,
            max_compress_tokens=request.max_compress_tokens,
            max_items_per_session=request.max_items_per_session,
        )
        orch = await _memory_engine.start_dreaming(
            scope_id=request.scope_id,
            user_id=request.user_id,
            config=config,
        )
        return {"started": orch is not None, "scope_id": request.scope_id, "user_id": request.user_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error starting dreaming: {str(e)}") from e


@router.post("/stop_dreaming")
async def stop_dreaming_endpoint(request: StopDreamingRequest):
    """Stop dreaming. With no args, stop everything; otherwise stop matching scope/user."""
    try:
        await _memory_engine.stop_dreaming(scope_id=request.scope_id, user_id=request.user_id)
        return {"success": True, "scope_id": request.scope_id, "user_id": request.user_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error stopping dreaming: {str(e)}") from e


@router.get("/dreaming/status")
async def dreaming_status_endpoint():
    """List all dreaming orchestrators with checkpoint info (running/interval/last_scan/sessions).

    Fields: running/interval_seconds from orchestrator.health; last_scan_ts/scanned_sessions_count
    from KV checkpoint dreaming/checkpoint/{scope}/{user}; next_estimated_ts = last_scan + interval
    (only when running); last_promoted_count = null (Sweeper 未记，待补)。
    """
    try:
        out = []
        for (scope_id, user_id), orch in _memory_engine.dreaming_orchestrators.items():
            h = orch.health
            running = h.get("running", False)
            interval = h.get("interval_seconds")
            entry = {
                "scope_id": scope_id,
                "user_id": user_id,
                "running": running,
                "interval_seconds": interval,
                "last_scan_ts": None,
                "next_estimated_ts": None,
                "scanned_sessions_count": 0,
                "last_promoted_count": None,
            }
            if _memory_engine.kv_store:
                ckpt_key = f"dreaming/checkpoint/{scope_id}/{user_id}"
                raw = await _memory_engine.kv_store.get(ckpt_key)
                if raw:
                    data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                    entry["last_scan_ts"] = data.get("last_scan_ts")
                    entry["scanned_sessions_count"] = len(data.get("scanned_sessions", []))
                    if entry["last_scan_ts"] and interval and running:
                        try:
                            last = datetime.fromisoformat(entry["last_scan_ts"])
                            nxt = last + timedelta(seconds=float(interval))
                            entry["next_estimated_ts"] = nxt.isoformat()
                        except (ValueError, TypeError) as exc:
                            memory_logger.warning(
                                "Failed to compute next_estimated_ts for "
                                "dreaming/%s/%s: %s",
                                scope_id, user_id, str(exc),
                            )
            out.append(entry)
        return {"orchestrators": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting dreaming status: {str(e)}") from e


# ===== 内核配置管理端点（配置中心，设计文档 §5.3.6）=====

@router.get("/admin/config")
async def get_admin_config():
    """Return current effective kernel config, grouped by category and already desensitized."""
    try:
        return _admin_config_payload()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading admin config: {str(e)}") from e


@router.put("/admin/config")
async def update_admin_config(request: AdminConfigUpdateRequest):
    """Persist editable kernel env params into .env without touching architecture params.

    设计文档 §5.3.6 实现建议：
    1. 接收 {"updates": {"KEY": "value", ...}} 请求体
    2. 校验每个 key 是否在允许修改的白名单内（架构参数拒绝）
    3. 校验 value 合法性（路径遍历、URL 格式、长度限制）
    4. 使用 python-dotenv 的 set_key() 写入 .env 文件
    """
    try:
        env_path = _resolve_env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_path.exists():
            env_path.touch()

        accepted: Dict[str, str] = {}
        rejected: Dict[str, str] = {}
        for raw_key, raw_value in request.updates.items():
            key = str(raw_key).strip().upper()
            if key in ARCHITECTURE_PARAMS:
                rejected[key] = "architecture_param"
                continue
            if key not in EDITABLE_ENV_PARAMS:
                rejected[key] = "unsupported_param"
                continue
            try:
                accepted[key] = _validate_config_value(key, raw_value)
            except Exception as exc:
                rejected[key] = str(exc)

        for key, value in accepted.items():
            set_key(str(env_path), key, value)
            os.environ[key] = value

        return {
            "status": "success",
            "updated_keys": list(accepted.keys()),
            "rejected_keys": list(rejected.keys()),
            "rejected": rejected,
            "env_path": str(env_path),
            "reload_required": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating admin config: {str(e)}") from e


@router.post("/admin/reload-config")
async def reload_admin_config():
    """Reload .env and rebuild the in-process memory engine without restarting the OS process."""
    try:
        await _reload_memory_engine()
        return {"status": "success", "message": "kernel config reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reloading admin config: {str(e)}") from e


@router.post("/admin/restart")
async def restart_admin():
    """Restart the kernel OS process via os.execv() (设计文档 §5.3.6).

    设计文档 §5.3.6 实现建议：
    1. 调用 shutdown_event() 优雅停止后台任务
    2. 通过 os.execv() 或 systemd/supervisor 触发进程重启
    3. 重启后 _load_env() 重新加载 .env，startup_event() 重建 store

    注意：os.execv() 会用新进程镜像替换当前进程，此函数不会返回。
    """
    try:
        # 1. 优雅停止后台任务
        try:
            await _shutdown_event()
        except Exception as exc:
            memory_logger.warning("Error during graceful shutdown before restart: %s", str(exc))

        # 2. 通过 os.execv() 重启进程（用相同的 Python 解释器和参数）
        memory_logger.info("Kernel restarting via os.execv()...", event_type=LogEventType.MEMORY_INIT)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # os.execv 不会返回；若到达此处说明 execv 失败
        raise RuntimeError("os.execv() failed to restart the process")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error restarting kernel: {str(e)}") from e


# ---------------------------------------------------------------------------
# 注册函数 — 由 memory_server.py 调用
# ---------------------------------------------------------------------------

def register_config_center_endpoints(
    app,
    *,
    memory_engine,
    env_value: Callable,
    bool_env: Callable,
    typed_env_value: Callable,
    resolve_env_path: Callable,
    reload_memory_engine: Callable,
    shutdown_event: Callable,
):
    """注册配置中心所有端点到 FastAPI app。

    参数由 memory_server.py 注入，避免循环导入：
    - memory_engine: LongTermMemory 单例
    - env_value / bool_env / typed_env_value: env 读取工具函数
    - resolve_env_path: 返回可写 .env 路径
    - reload_memory_engine: async，重建引擎
    - shutdown_event: async，优雅停止后台任务
    """
    global _memory_engine, _env_value, _bool_env, _typed_env_value
    global _resolve_env_path, _reload_memory_engine, _shutdown_event
    _memory_engine = memory_engine
    _env_value = env_value
    _bool_env = bool_env
    _typed_env_value = typed_env_value
    _resolve_env_path = resolve_env_path
    _reload_memory_engine = reload_memory_engine
    _shutdown_event = shutdown_event
    app.include_router(router)
