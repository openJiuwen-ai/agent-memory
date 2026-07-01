import os
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.schema.param import Param
from jiuwen_memory.foundation.llm import BaseMessage
from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwen_memory.memory_core.config.config import AgentMemoryConfig, MemoryEngineConfig, MemoryScopeConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType
from jiuwen_memory.retrieval.common.config import EmbeddingConfig
from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding
from jiuwen_memory.server.store_factory import (
    create_async_engine_from_env,
    create_db_store,
    create_kv_store,
    create_vector_store,
)

# 加载 .env 文件 — fallback 链: ~/.jiuwenmemory/.env → 当前工作目录/.env
_JIUWEN_DIR = Path.home() / ".jiuwenmemory"


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


class AddMessagesRequest(BaseModel):
    messages: list[dict[str, str]]
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    # 可选：调用方传入需抽取的变量定义，直接复用引擎的 Param（对齐 AgentMemoryConfig.mem_variables）；
    # 不传则 mem_variables 为空（不抽取变量）
    mem_variables: list[Param] = Field(default_factory=list)
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


class GetUserMemByPageRequest(BaseModel):
    user_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    scope_id: Optional[str] = LongTermMemory.DEFAULT_VALUE
    page_size: int = 10
    page_idx: int = 1
    memory_type: Optional[str] = "UNKNOWN"  # 对应MemoryType枚举值

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
        engine = create_async_engine_from_env()
        kv_store = create_kv_store(engine)
        db_store = create_db_store(engine)
        vector_store = create_vector_store()

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
            embedding_model=embedding_model
        )

        # 创建配置 - 使用默认配置
        config = MemoryEngineConfig(
            default_model_cfg=ModelRequestConfig(
                model=os.getenv("MODEL_NAME", "")
            ),
            default_model_client_cfg=ModelClientConfig(
                client_provider=os.getenv("MODEL_PROVIDER", ""),
                api_key=os.getenv("API_KEY", ""),
                api_base=os.getenv("API_BASE", ""),
                verify_ssl=False
            )
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



@app.post("/add_messages/")
async def add_messages_endpoint(request: AddMessagesRequest):
    """添加消息到内存"""
    try:
        # 转换请求中的消息为BaseMessage对象
        base_messages = []
        for msg in request.messages:
            base_messages.append(BaseMessage(role=msg.get('role', 'user'), content=msg.get('content', '')))
        # mem_variables 已由 pydantic 反序列化为 Param 列表，直接透传给 AgentMemoryConfig；
        # 未传则 mem_variables 为空（不抽取变量）
        agent_cfg = AgentMemoryConfig(
            mem_variables=request.mem_variables,
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
            "POST /get_variables/",
            "POST /search_memory/",
            "POST /search_user_history_summary/",
            "POST /get_user_mem_by_page/",
            "GET /health"
        ]
    }


def main():
    """CLI entry point: start the Memory Server via uvicorn."""
    import sys
    import uvicorn

    # 从环境变量获取主机地址和端口，默认为 127.0.0.1:8000（仅本地监听）
    host = os.getenv("IP", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    # 安全守卫：绑定非本机地址必须配置 MEMORY_API_KEY
    if not host.startswith("127.") and host != "localhost" and not MEMORY_API_KEY:
        memory_logger.error(
            "IP=%s exposes the service to the network, but MEMORY_API_KEY is not set. "
            "Set MEMORY_API_KEY in ~/.jiuwenmemory/.env, or use IP=127.0.0.1 for local-only access.",
            host,
        )
        sys.exit(1)

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
