import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.schema.param import Param, ParamType
from jiuwen_memory.foundation.llm import BaseMessage
from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwen_memory.memory_core.config.config import (
    AgentMemoryConfig,
    MemoryEngineConfig,
    MemoryScopeConfig,
)
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType
from jiuwen_memory.retrieval.common.config import EmbeddingConfig
from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding
from jiuwen_memory.server.config_center_api import register_config_center_endpoints
from jiuwen_memory.server.log_center_api import register_log_center_endpoints
from jiuwen_memory.server.store_factory import (
    create_async_engine_from_env,
    create_db_store,
    create_file_root_dir,
    create_kv_store,
    create_vector_store,
)

# 加载 .env 文件 — fallback 链: ~/.jiuwenmemory/.env → 当前工作目录/.env
_JIUWEN_DIR = Path.home() / ".jiuwenmemory"
_reload_lock = asyncio.Lock()


def _load_env() -> None:
    """按优先级加载 .env，找不到时自动创建 ~/.jiuwenmemory/ 目录。"""
    # 优先 ~/.jiuwenmemory/.env
    home_env = _JIUWEN_DIR / ".env"
    if home_env.exists():
        load_dotenv(dotenv_path=str(home_env), override=True)
        memory_logger.info("Loaded .env from: %s", str(home_env))
        return

    # 其次 当前工作目录/.env
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(dotenv_path=str(cwd_env), override=True)
        memory_logger.info("Loaded .env from current directory: %s", str(cwd_env))
        return

    # 都没找到 — 自动创建 ~/.jiuwenmemory/ 目录，并提示用户配置
    _JIUWEN_DIR.mkdir(parents=True, exist_ok=True)
    memory_logger.warning(
        "No .env found in %s or current directory. "
        "Please create %s/.env with your configuration. "
        "A template is available in the server/.env.example file of the source repository.",
        str(_JIUWEN_DIR),
        str(_JIUWEN_DIR),
    )


def _resolve_env_path() -> Path:
    """Return the writable .env path following the same fallback order as _load_env."""
    home_env = _JIUWEN_DIR / ".env"
    if home_env.exists():
        return home_env
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    _JIUWEN_DIR.mkdir(parents=True, exist_ok=True)
    return home_env


def _env_value(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _bool_env(key: str, default: bool = False) -> bool:
    return _env_value(key, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


def _typed_env_value(key: str, default: Any = "") -> Any:
    raw = _env_value(key, "" if default is None else str(default))
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


async def _reload_memory_engine() -> None:
    """Rebuild stores/config from the latest .env and swap the in-process engine.

    Concurrency-safe reload order (Committer Review §3.7):
    1. Create the new engine first (while old engine still serves requests).
    2. Atomically swap the global reference so concurrent requests see the new
       engine immediately.
    3. Stop the old engine only after the swap, so no request ever hits a
       stopped engine.

    注意：LongTermMemory 使用 Singleton metaclass，old_engine 和 new_engine 是同一对象，
    故不会真正停止旧引擎——register_store 会重新装配 stores，set_config 会重置配置。
    file watcher 需先停止再重启，避免重复 watcher 线程。
    """
    global memory_engine, MEMORY_API_KEY
    async with _reload_lock:
        _load_env()
        MEMORY_API_KEY = _env_value("MEMORY_API_KEY", "")
        # 停止 file watcher，避免 register_store 重复启动
        try:
            idx = memory_engine.memory_index
            if idx is not None and hasattr(idx, "stop_watcher"):
                idx.stop_watcher()
        except Exception as exc:
            memory_logger.warning("Failed to stop file watcher before reload: %s", str(exc))
        # 通过 store_factory 根据 .env 装配 KV / DB / Vector store
        engine = create_async_engine_from_env()
        kv_store = create_kv_store(engine)
        db_store = create_db_store(engine)

        # index_backend 决定 memory_index 后端：simple（缺省，向量）/ file（markdown+SQLite）
        index_backend = _env_value("INDEX_BACKEND", "simple").strip().lower()
        file_root_dir = None
        if index_backend == "file":
            file_root_dir = create_file_root_dir()

        # vector_store 在 simple 模式作 memory_index 后端（无条件创建，缺省 chroma）；
        # 在 file 模式仅给中间记忆 / dreaming 的 SemanticStore 用，不配 VECTOR_STORE_TYPE 时为 None
        if index_backend == "simple":
            vector_store = create_vector_store()
        else:
            vector_store = create_vector_store() if os.getenv("VECTOR_STORE_TYPE", "").strip() else None

        embedding_model = APIEmbedding(
            config=EmbeddingConfig(
                model_name=_env_value("EMBED_MODEL_NAME", ""),
                api_key=_env_value("EMBED_API_KEY", ""),
                base_url=_env_value("EMBED_API_BASE", "")
            )
        )

        # 注册存储（直接复用全局 memory_engine 单例，Singleton 下 register_store 会重新装配 stores）
        await memory_engine.register_store(
            kv_store=kv_store,
            db_store=db_store,
            vector_store=vector_store,
            embedding_model=embedding_model,
            index_backend=index_backend,
            file_root_dir=file_root_dir,
        )

        # 从环境变量构造 MemoryEngineConfig（适配 jiuwen_memory 的字段集）并重置配置
        crypto_key_str = _env_value("CRYPTO_KEY", "")
        config = MemoryEngineConfig(
            default_model_cfg=ModelRequestConfig(
                model=_env_value("MODEL_NAME", "default-model"),
                temperature=_typed_env_value("TEMPERATURE", 0.95),
            ),
            default_model_client_cfg=ModelClientConfig(
                client_provider=_env_value("MODEL_PROVIDER", "SiliconFlow"),
                api_key=_env_value("API_KEY", ""),
                api_base=_env_value("API_BASE", ""),
                verify_ssl=_bool_env("MODEL_SSL_VERIFY", False),
            ),
            enable_middle_memory=_bool_env("MEMORY_ENABLE_MIDDLE_MEMORY", False),
            middle_memory_check_interval=int(_env_value("MEMORY_MIDDLE_CHECK_INTERVAL", "50")),
            crypto_key=crypto_key_str.encode("utf-8") if crypto_key_str else b"",
        )
        memory_engine.set_config(config)


_load_env()

# 读取 API Key（空字符串 = 不启用鉴权，仅限本地开发）
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

app = FastAPI(
    title="Memory Engine API",
    description="API for managing long-term memory operations",
    version="1.0.0"
)


# 鉴权中间件：若配置了 MEMORY_API_KEY，所有 POST / PUT / DELETE 请求必须携带 Authorization: Bearer <key>
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # GET 端点始终放行（health check、root）
    if request.method == "GET":
        return await call_next(request)
    # 未配置 key 时放行所有请求（本地开发模式）
    if not MEMORY_API_KEY:
        return await call_next(request)
    # 校验 Authorization header
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {MEMORY_API_KEY}":
        return JSONResponse(status_code=401, content={"detail": "Unauthorized: invalid or missing API key"})
    return await call_next(request)

# 初始化内存引擎实例
memory_engine = LongTermMemory()


class MessageRequest(BaseModel):
    role: str
    content: str


class MemVariable(BaseModel):
    """/add_messages/ 变量抽取定义的对外窄模型。

    引擎内部 ``AgentMemoryConfig.mem_variables`` 复用通用 ``Param``，而 ``Param`` 把
    ``type``/``required`` 声明为必填（``Field(...)``）——这是为 Agent/工作流参数定义设计的强校验。
    但变量抽取场景调用方只需 ``name``+``description``，引擎（memory_analyzer）也只读这两个字段。
    故对外只要求 name/description 必填，type/required/default 可选带默认，端点内补齐成 Param。

    type 仅支持简单类型（string/boolean/integer/number）——变量抽取是扁平 key/value 结构，
    引擎不支持 Array/Object 嵌套类型；传入 array/object 或未知值在反序列化阶段即 422，
    避免落到 to_param 才 500。extra='forbid' 拒收未知字段，防止字段名拼错被静默吞掉。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="变量名")
    description: str = Field(..., description="变量描述")
    type: str = Field("string", description="变量类型，默认 string；仅支持 string/boolean/integer/number")
    required: bool = Field(True, description="是否必填，默认 True")
    default: Optional[Any] = Field(None, description="默认值")

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        """仅允许简单类型；Array/Object/未知值在反序列化阶段即拒绝（422）。"""
        allowed = {ParamType.String, ParamType.Boolean, ParamType.Integer, ParamType.Number}
        try:
            pt = ParamType(v)
        except ValueError:
            raise ValueError(
                f"mem_variables type '{v}' 不合法，仅支持 string/boolean/integer/number"
            ) from None
        if pt not in allowed:
            raise ValueError(
                f"mem_variables type '{v}' 不支持，变量抽取仅支持简单类型 string/boolean/integer/number"
            )
        return v

    def to_param(self) -> Param:
        """补齐 type/required 默认值，构造引擎要求的 Param。type 已由校验保证为简单类型。"""
        return Param(
            name=self.name,
            description=self.description,
            type=ParamType(self.type),
            required=self.required,
            default=self.default,
        )


class AddMessagesRequest(BaseModel):
    messages: list[dict[str, str]]
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    session_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    # 可选：调用方传入需抽取的变量定义；对外用窄模型 MemVariable（只要求 name/description），
    # 端点内适配成引擎的 Param。不传则 mem_variables 为空（不抽取变量）
    mem_variables: list[MemVariable] = Field(default_factory=list)
    # 抽取开关，默认与引擎保持一致（全开），调用方可按需关闭某类记忆抽取
    enable_long_term_mem: bool = Field(default=True)
    enable_user_profile: bool = Field(default=True)
    enable_semantic_memory: bool = Field(default=True)
    enable_episodic_memory: bool = Field(default=True)
    enable_summary_memory: bool = Field(default=True)


class UpdateMemoryRequest(BaseModel):
    mem_id: str
    memory: str
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class UpdateVariablesRequest(BaseModel):
    variables: Dict[str, str]
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class DeleteVariablesRequest(BaseModel):
    names: List[str]
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class DeleteByScopeRequest(BaseModel):
    scope_id: str


class DeleteMemByIdRequest(BaseModel):
    mem_id: str
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class BatchDeleteMemRequest(BaseModel):
    mem_ids: List[str]
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class GetVariablesRequest(BaseModel):
    names: Optional[List[str]] = None
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE


class SearchMemoryRequest(BaseModel):
    query: str
    num: int = 10
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    threshold: Optional[float] = 0.3


class SearchUserHistorySummaryRequest(BaseModel):
    query: str
    num: int = 10
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    threshold: Optional[float] = 0.3


# page_size 入参校验常量
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


class GetUserMemByPageRequest(BaseModel):
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    page_size: int = DEFAULT_PAGE_SIZE
    page_idx: int = 1
    memory_type: Optional[str] = "UNKNOWN"  # 对应MemoryType枚举值

    @field_validator("page_size", mode="before")
    @classmethod
    def _validate_page_size(cls, value):
        """page_size 入参校验与兜底处理。

        严格拒绝负数、零值及非数值类型：命中时记录日志并回退到默认值
        DEFAULT_PAGE_SIZE；对超出 MAX_PAGE_SIZE 的极大值做上限钳制，
        避免非法/越界参数透传给底层 Elasticsearch 触发 BadRequestError(400)，
        继而被端点错误包装为 500 Internal Server Error 的问题。
        """
        # 缺省值 None → 默认
        if value is None:
            memory_logger.warning(
                "page_size is None, fallback to default %d", DEFAULT_PAGE_SIZE
            )
            return DEFAULT_PAGE_SIZE

        # bool 是 int 子类，先排除，视作非数值类型
        if isinstance(value, bool):
            memory_logger.warning(
                "page_size got non-numeric type bool(%s), fallback to default %d",
                value, DEFAULT_PAGE_SIZE,
            )
            return DEFAULT_PAGE_SIZE

        # 尝试转为 int（兼容 "10" / 10.0 等数值形态）；失败则兜底
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            memory_logger.warning(
                "page_size got non-numeric value %r, fallback to default %d",
                value, DEFAULT_PAGE_SIZE,
            )
            return DEFAULT_PAGE_SIZE

        # 负数 / 零：拒绝并兜底
        if numeric <= 0:
            memory_logger.warning(
                "page_size %d invalid (must be positive), fallback to default %d",
                numeric, DEFAULT_PAGE_SIZE,
            )
            return DEFAULT_PAGE_SIZE

        # 极大值：上限钳制，避免触发底层搜索引擎 size 上限
        if numeric > MAX_PAGE_SIZE:
            memory_logger.warning(
                "page_size %d exceeds max %d, clamped to %d",
                numeric, MAX_PAGE_SIZE, MAX_PAGE_SIZE,
            )
            return MAX_PAGE_SIZE

        return numeric

    def get_memory_type_enum(self):
        """将字符串转换为MemoryType枚举"""
        try:
            return MemoryType(self.memory_type.lower())
        except ValueError:
            return MemoryType.UNKNOWN


@app.on_event("startup")
async def startup_event():
    """Initialize the memory engine with stores and configuration."""
    try:
        # 通过 store_factory 根据 .env 装配 KV / DB / Vector store
        # KV 与 DB 解耦为两个独立 engine（指向同一 sqlite_db.db，WAL 下并发安全）：
        # KV 承载 DistributedLock（高频短写：每 3s renew_exclusive）+ 配置键，
        # DB 承载 message_store（长事务：add_messages 多次写 + LLM 提取）。
        # 共用 engine 时二者抢同一把库级写锁，长稳 1h 后写者堆积、5s busy_timeout
        # 顶不住开始掉 500。解耦后两连接池独立，短事务与长事务不再互相同步等待。
        kv_engine = create_async_engine_from_env()
        db_engine = create_async_engine_from_env()
        kv_store = create_kv_store(kv_engine)
        db_store = create_db_store(db_engine)

        # index_backend 决定 memory_index 后端：simple（缺省，向量）/ file（markdown+SQLite）
        index_backend = os.getenv("INDEX_BACKEND", "simple").strip().lower()
        # file 后端的数据目录
        file_root_dir = None
        if index_backend == "file":
            file_root_dir = create_file_root_dir()

        # vector_store 在 simple 模式作 memory_index 后端（无条件创建，缺省 chroma，
        # 保持原有行为）；在 file 模式仅给中间记忆 / dreaming 的 SemanticStore 用，
        # 不配 VECTOR_STORE_TYPE 时为 None（此时中间记忆/dreaming 不可用）
        if index_backend == "simple":
            vector_store = create_vector_store()
        else:
            vector_store = create_vector_store() if os.getenv("VECTOR_STORE_TYPE", "").strip() else None

        embedding_model = APIEmbedding(
            config=EmbeddingConfig(
                model_name=os.getenv("EMBED_MODEL_NAME", ""),
                api_key=os.getenv("EMBED_API_KEY", ""),
                base_url=os.getenv("EMBED_API_BASE", "")
            )
        )

        # 注册存储
        await memory_engine.register_store(
            kv_store=kv_store,
            db_store=db_store,
            vector_store=vector_store,
            embedding_model=embedding_model,
            index_backend=index_backend,
            file_root_dir=file_root_dir,
        )

        # 创建配置 - 支持从.env读取加密配置
        codec_name = os.getenv("MEMORY_CODEC", "").strip().lower()
        crypto_key = b""
        
        # 根据加密算法类型处理密钥
        if codec_name == "sm4":
            # SM4国密算法：需要从.env读取SM4_KEY并注册codec
            from jiuwen_memory.foundation.codec import register_storage_codec, SM4StorageCodec
            sm4_key_hex = os.getenv("SM4_KEY", "")
            if sm4_key_hex:
                try:
                    sm4_key = bytes.fromhex(sm4_key_hex)
                    register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))
                    memory_logger.info("SM4 codec registered successfully")
                except Exception as e:
                    memory_logger.error(
                        "Failed to register SM4 codec: %s, falling back to plaintext",
                        str(e),
                    )
                    codec_name = ""  # 回退到明文模式
            else:
                memory_logger.warning(
                    "SM4 codec specified but SM4_KEY not set, falling back to plaintext"
                )
                codec_name = ""  # 回退到明文模式
        elif codec_name == "aes":
            # AES加密算法：从.env读取AES_KEY
            aes_key_hex = os.getenv("AES_KEY", "")
            if aes_key_hex:
                try:
                    crypto_key = bytes.fromhex(aes_key_hex)
                    memory_logger.info("AES crypto_key loaded from environment")
                except Exception as e:
                    memory_logger.error(
                        "Failed to parse AES_KEY: %s, using empty key",
                        str(e),
                    )
                    crypto_key = b""
            else:
                memory_logger.warning("AES codec specified but AES_KEY not set, using empty key")
                crypto_key = b""
        
        config = MemoryEngineConfig(
            default_model_cfg=ModelRequestConfig(
                model=os.getenv("MODEL_NAME", ""),
                temperature=_typed_env_value("TEMPERATURE", 0.95),
            ),
            default_model_client_cfg=ModelClientConfig(
                client_provider=os.getenv("MODEL_PROVIDER", ""),
                api_key=os.getenv("API_KEY", ""),
                api_base=os.getenv("API_BASE", ""),
                # 是否校验 LLM API 的 TLS 证书；默认不校验，设为 true 时开启
                verify_ssl=os.getenv("MODEL_SSL_VERIFY", "false").strip().lower() == "true"
            ),
            # 是否启用中期记忆；默认不启用，设为 true 时开启
            enable_middle_memory=_bool_env("MEMORY_ENABLE_MIDDLE_MEMORY", False),
            codec=codec_name,
            crypto_key=crypto_key
        )

        memory_engine.set_config(config)

        memory_logger.info("Memory engine initialized successfully")
        if not MEMORY_API_KEY:
            memory_logger.warning(
                "MEMORY_API_KEY is not set — API is running without authentication. "
                "Set MEMORY_API_KEY in ~/.jiuwenmemory/.env if the service is exposed to a network."
            )
    except Exception as e:
        memory_logger.error("Error initializing memory engine: %s", str(e))
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the file watcher and background tasks (symmetric with startup).

    file 后端在 register_store 内启动了 watchdog，进程退出时需对称停止，
    避免 Observer 后台线程挂到 join timeout。watchdog 未安装或未启动时
    stop_watcher 为 no-op，安全。
    """
    try:
        idx = memory_engine.memory_index
        if idx is not None and hasattr(idx, "stop_watcher"):
            idx.stop_watcher()
    except Exception as e:
        memory_logger.warning("Failed to stop file watcher on shutdown: %s", str(e))
    try:
        await memory_engine.stop()
        memory_logger.info("Memory engine background tasks stopped")
    except Exception as e:
        memory_logger.warning("Failed to stop memory engine: %s", str(e))


@app.post("/add_messages/")
async def add_messages_endpoint(request: AddMessagesRequest):
    """添加消息到内存"""
    try:
        # 转换请求中的消息为BaseMessage对象
        base_messages = []
        for msg in request.messages:
            base_messages.append(BaseMessage(role=msg.get('role', 'user'), content=msg.get('content', '')))
        # 对外用窄模型 MemVariable（只要求 name/description），这里补齐成引擎要求的 Param 列表；
        # 未传则 mem_variables 为空（不抽取变量）
        mem_variables = [v.to_param() for v in request.mem_variables]
        agent_cfg = AgentMemoryConfig(
            mem_variables=mem_variables,
            enable_long_term_mem=request.enable_long_term_mem,
            enable_user_profile=request.enable_user_profile,
            enable_semantic_memory=request.enable_semantic_memory,
            enable_episodic_memory=request.enable_episodic_memory,
            enable_summary_memory=request.enable_summary_memory,
        )

        await memory_engine.add_messages(
            messages=base_messages,
            agent_config=agent_cfg,
            user_id=request.user_id,
            scope_id=request.scope_id,
            session_id=request.session_id,
        )

        return {"status": "success", "message": "Messages added successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error adding messages: {str(e)}") from e


@app.post("/update_mem_by_id/")
async def update_mem_by_id_endpoint(request: UpdateMemoryRequest):
    """根据ID更新内存内容"""
    try:
        await memory_engine.update_mem_by_id(
            mem_id=request.mem_id,
            memory=request.memory,
            user_id=request.user_id,
            scope_id=request.scope_id
        )

        return {"status": "success", "message": f"Memory {request.mem_id} updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating memory: {str(e)}") from e


@app.post("/update_variables/")
async def update_variables_endpoint(request: UpdateVariablesRequest):
    """更新用户变量"""
    try:
        await memory_engine.update_variables(
            variables=request.variables,
            user_id=request.user_id,
            scope_id=request.scope_id
        )

        return {"status": "success", "message": "Variables updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating variables: {str(e)}") from e


@app.post("/delete_variables/")
async def delete_variables_endpoint(request: DeleteVariablesRequest):
    """删除用户变量"""
    try:
        result = await memory_engine.delete_variables(
            names=request.names,
            user_id=request.user_id,
            scope_id=request.scope_id
        )

        return {"status": "success", "deleted": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting variables: {str(e)}") from e


@app.post("/delete_mem_by_scope/")
async def delete_mem_by_scope_endpoint(request: DeleteByScopeRequest):
    """删除特定范围内的所有记忆"""
    try:
        result = await memory_engine.delete_mem_by_scope(scope_id=request.scope_id)

        return {"status": "success", "deleted": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting memory by scope: {str(e)}") from e


@app.post("/get_variables/")
async def get_variables_endpoint(request: GetVariablesRequest):
    """获取用户变量"""
    try:
        result = await memory_engine.get_variables(
            names=request.names,
            user_id=request.user_id,
            scope_id=request.scope_id
        )

        return {"variables": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting variables: {str(e)}") from e


@app.post("/search_memory/")
async def search_memory_endpoint(request: SearchMemoryRequest):
    """搜索内存内容"""
    try:
        results = await memory_engine.search_user_mem(
            query=request.query,
            num=request.num,
            user_id=request.user_id,
            scope_id=request.scope_id,
            threshold=request.threshold
        )

        # 将结果转换为可序列化的格式
        serializable_results = []
        for result in results:
            serializable_results.append({
                "mem_id": result.mem_info.mem_id,
                "content": result.mem_info.content,
                "type": result.mem_info.type.value if hasattr(result.mem_info.type, 'value') else str(
                    result.mem_info.type),
                "score": result.score
            })

        return {"results": serializable_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching memory: {str(e)}") from e


@app.post("/search_user_history_summary/")
async def search_user_history_summary_endpoint(request: SearchUserHistorySummaryRequest):
    """搜索用户历史摘要"""
    try:
        results = await memory_engine.search_user_history_summary(
            query=request.query,
            num=request.num,
            user_id=request.user_id,
            scope_id=request.scope_id,
            threshold=request.threshold
        )

        # 将结果转换为可序列化的格式
        serializable_results = []
        for result in results:
            serializable_results.append({
                "mem_id": result.mem_info.mem_id,
                "content": result.mem_info.content,
                "type": result.mem_info.type.value if hasattr(result.mem_info.type, 'value') else str(
                    result.mem_info.type),
                "score": result.score
            })

        return {"results": serializable_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching user history summary: {str(e)}") from e


@app.post("/get_user_mem_by_page/")
async def get_user_mem_by_page_endpoint(request: GetUserMemByPageRequest):
    """分页获取用户内存"""
    try:
        # 获取MemoryType枚举
        memory_type_enum = request.get_memory_type_enum()

        results = await memory_engine.get_user_mem_by_page(
            user_id=request.user_id,
            scope_id=request.scope_id,
            page_size=request.page_size,
            page_idx=request.page_idx,
            memory_type=memory_type_enum
        )

        # 将结果转换为可序列化的格式
        serializable_results = []
        for result in results:
            serializable_results.append({
                "mem_id": result.mem_id,
                "content": result.content,
                "type": result.type.value if hasattr(result.type, 'value') else str(result.type)
            })

        return {"results": serializable_results, "total": len(serializable_results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting user memory by page: {str(e)}") from e


@app.post("/get_user_mem_by_page_with_total/")
async def get_user_mem_by_page_with_total_endpoint(request: GetUserMemByPageRequest):
    """分页获取用户内存（返回真实全局 total，用于平台分页）"""
    try:
        memory_type_enum = request.get_memory_type_enum()

        results, total = await memory_engine.get_user_mem_by_page_with_total(
            user_id=request.user_id,
            scope_id=request.scope_id,
            page_size=request.page_size,
            page_idx=request.page_idx,
            memory_type=memory_type_enum
        )

        serializable_results = []
        for result in results:
            serializable_results.append({
                "mem_id": result.mem_id,
                "content": result.content,
                "type": result.type.value if hasattr(result.type, 'value') else str(result.type),
                "timestamp": result.timestamp.isoformat() if result.timestamp else None,
                "source_id": result.source_id
            })

        return {"results": serializable_results, "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting user memory by page: {str(e)}") from e


# ---------------------------------------------------------------------------
# 注册配置中心 + 日志中心端点（抽离自本文件，见 config_center_api.py / log_center_api.py）
# ---------------------------------------------------------------------------

register_config_center_endpoints(
    app,
    memory_engine=memory_engine,
    env_value=_env_value,
    bool_env=_bool_env,
    typed_env_value=_typed_env_value,
    resolve_env_path=_resolve_env_path,
    reload_memory_engine=_reload_memory_engine,
    shutdown_event=shutdown_event,
)

register_log_center_endpoints(
    app,
    memory_engine=memory_engine,
)

# 注册批量删除管理端点
from jiuwen_memory.server.mem_meta_api import register_mem_meta_endpoints
register_mem_meta_endpoints(
    app,
    memory_engine=memory_engine,
    milvus_uri=os.getenv("VECTOR_MILVUS_URI", "http://localhost:8530"),
    db_path=os.getenv("MEM_META_DB_PATH", "/tmp/milvus_memory_metadata.db"),
)


@app.post("/delete_mem_by_id/")
async def delete_mem_by_id_endpoint(request: DeleteMemByIdRequest):
    """根据ID删除单条记忆"""
    try:
        await memory_engine.delete_mem_by_id(
            mem_id=request.mem_id,
            user_id=request.user_id,
            scope_id=request.scope_id,
        )
        return {"status": "success", "message": f"Memory {request.mem_id} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting memory by id: {str(e)}") from e


@app.post("/batch_delete_mem/")
async def batch_delete_mem_endpoint(request: BatchDeleteMemRequest):
    """批量删除记忆（向量存储批量删 + KV 批量删，非逐条 HTTP）"""
    try:
        if not request.mem_ids:
            return {"status": "success", "deleted": 0, "failed": 0}

        deleted, failed = 0, 0
        errors: list[dict] = []
        from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock
        lock = DistributedLock(memory_engine.kv_store, f"user/{request.user_id}")
        async with lock:
            if memory_engine.memory_index:
                try:
                    await memory_engine.memory_index.delete_memories(
                        user_id=request.user_id,
                        scope_id=request.scope_id,
                        ids=request.mem_ids
                    )
                    deleted = len(request.mem_ids)
                except Exception:
                    # 批量删失败，降级逐条删
                    for mid in request.mem_ids:
                        try:
                            await memory_engine.delete_mem_by_id(
                                mem_id=mid,
                                user_id=request.user_id,
                                scope_id=request.scope_id
                            )
                            deleted += 1
                        except Exception as e:
                            failed += 1
                            errors.append({"mem_id": mid, "error": str(e)})
            else:
                for mid in request.mem_ids:
                    try:
                        await memory_engine.delete_mem_by_id(
                            mem_id=mid,
                            user_id=request.user_id,
                            scope_id=request.scope_id
                        )
                        deleted += 1
                    except Exception as e:
                        failed += 1
                        errors.append({"mem_id": mid, "error": str(e)})

        return {"status": "success", "deleted": deleted, "failed": failed, "errors": errors}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error batch deleting memory: {str(e)}") from e


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "message": "Memory Engine API is running"}


@app.get("/")
async def root():
    """根端点"""
    return {
        "message": "Welcome to Memory Engine API",
        "endpoints": [
            "POST /add_messages/",
            "POST /update_mem_by_id/",
            "POST /update_variables/",
            "POST /delete_variables/",
            "POST /delete_mem_by_scope/",
            "POST /delete_mem_by_id/",
            "POST /batch_delete_mem/",
            "POST /get_variables/",
            "POST /search_memory/",
            "POST /search_user_history_summary/",
            "POST /get_user_mem_by_page/",
            "POST /get_message_by_id/",
            "POST /set_scope_config",
            "POST /get_scope_config",
            "POST /delete_scope_config",
            "POST /start_dreaming",
            "POST /stop_dreaming",
            "GET /dreaming/status",
            "GET /admin/config",
            "PUT /admin/config",
            "POST /admin/reload-config",
            "POST /admin/restart",
            "POST /admin/messages/query",
            "GET /admin/messages/stats",
            "GET /admin/messages/export",
            "GET /admin/messages/detail/{msg_id}",
            "GET /logs/tail",
            "GET /logs/download",
            "GET /logs/files",
            "POST /get_user_mem_by_page_with_total/",
            "GET /health"
        ]
    }


def is_host_exposed_without_key(host: str) -> bool:
    """安全守卫判断：绑定非本机地址且未配置 MEMORY_API_KEY 时返回 True（应拒绝启动）。

    抽成纯函数便于单测：测试无需触发 sys.exit，只断言本函数返回值。
    本机地址（127.* / localhost）即使无 key 也放行，仅限本地开发。
    """
    is_local = host.startswith("127.") or host == "localhost"
    return not is_local and not MEMORY_API_KEY


def main():
    """CLI entry point: start the Memory Server via uvicorn."""
    import uvicorn  # noqa: E402  — deferred import keeps server startup lazy

    # 从环境变量获取主机地址和端口，默认为 127.0.0.1:8000（仅本地监听）
    host = os.getenv("IP", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    # 安全守卫：绑定非本机地址必须配置 MEMORY_API_KEY
    if is_host_exposed_without_key(host):
        memory_logger.error(
            "IP=%s exposes the service to the network, but MEMORY_API_KEY is not set. "
            "Set MEMORY_API_KEY in ~/.jiuwenmemory/.env, or use IP=127.0.0.1 for local-only access.",
            host,
        )
        sys.exit(1)

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
