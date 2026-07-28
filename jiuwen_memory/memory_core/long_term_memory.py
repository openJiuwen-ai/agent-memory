# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import copy
import json
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from jiuwen_memory.foundation.store.filter_dsl import FilterGroup

from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
from jiuwen_memory.foundation.llm import Model, UserMessage, AssistantMessage, BaseMessage
from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock
from jiuwen_memory.memory_core.config.config import (
    MemoryEngineConfig,
    MemoryScopeConfig,
    AgentMemoryConfig,
    DreamingConfig,
    ForgettingConfig,
)
from jiuwen_memory.memory_core.process.extract.generation import Generator
from jiuwen_memory.memory_core.process.dreaming import (
    DreamingOrchestrator,
    MemoryUnitKnowledgeStore,
    MessageStoreSessionSource,
    Sweeper,
)
from jiuwen_memory.memory_core.process.forgetting import (
    EbbinghausEvaluator,
    ForgetContext,
)
from jiuwen_memory.memory_core.manage.mem_model.data_id_manager import DataIdManager
from jiuwen_memory.memory_core.manage.mem_model.message_manager import MessageManager, MessageAddRequest
from jiuwen_memory.foundation.store.base_message_store import BaseMessageStore
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager
from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
from jiuwen_memory.memory_core.manage.index.write_manager import WriteManager
from jiuwen_memory.memory_core.manage.index.summary_manager import SummaryManager
from jiuwen_memory.memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager, MemoryType
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import FragmentMemoryUnit, MemoryType,\
    SummaryUnit, VariableUnit
from jiuwen_memory.memory_core.manage.search.search_manager import SearchManager, SearchParams
from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.memory_core.manage.mem_model.db_model import create_tables
from jiuwen_memory.memory_core.manage.mem_model.sql_db_store import SqlDbStore
from jiuwen_memory.memory_core.manage.mem_model.sql_message_store import SqlMessageStore
from jiuwen_memory.foundation.llm import UserMessage, AssistantMessage, BaseMessage, Model
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.retrieval.embedding.base import Embedding
from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding
from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
from jiuwen_memory.memory_core.manage.mem_model.scope_user_mapping_manager import ScopeUserMappingManager
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import BaseError, build_error
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType
from jiuwen_memory.memory_core.migration.run_migrations import run_kv_migrations,\
    run_vector_migrations, run_sql_migrations, run_message_migrations
from jiuwen_memory.foundation.codec import (
    AesStorageCodec,
    StorageCodec,
    get_default_registry,
)
from jiuwen_memory.memory_core.manage.mem_model.semantic_store import SemanticStore
from jiuwen_memory.memory_core.common.kv_prefix_registry import kv_prefix_registry


# KV key prefix for the per-mem retrieve_history records written by
# ``SearchManager._append_retrieve_history`` (search path) and seeded /
# cleaned up by ``LongTermMemory.add_memory_unit`` / ``delete_mem_by_id``.
# Co-located with the user_var / session_var prefixes so the KV migrator
# / batch cleaner picks it up automatically. Layout of a single key:
#   retrieve_history{SEP}{user_id}{SEP}{scope_id}{SEP}{mem_id}
# where SEP is ``VariableManager.SEPARATOR`` (currently ``/``).
RETRIEVE_HISTORY_PREFIX = "retrieve_history"

# retrieve_history is a FIFO rolling window: once it reaches
# _RETRIEVE_HISTORY_MAX_LEN entries, the oldest (front) entry is
# dropped before the new ts is appended. Keeps KV payload bounded and
# favours recency — which is what the Ebbinghaus reinforcement term
# (σ · Σ 1/d) cares about.
_RETRIEVE_HISTORY_MAX_LEN = 20


class MemInfo(BaseModel):
    mem_id: str = Field(default="", description="memory id")
    content: str = Field(default="", description="memory content")
    type: MemoryType = Field(default=MemoryType.USER_PROFILE, description="memory type")
    timestamp: datetime | None = Field(default=None, description="memory timestamp")


class MemResult(BaseModel):
    mem_info: MemInfo = Field(default=None, description="memory information")
    score: float = Field(default=0.0, description="memory score of relevance")


class AddMemResult(BaseModel):
    variables: list[VariableUnit] = Field(default_factory=list, description="variables result")
    user_profile: list[FragmentMemoryUnit] = Field(default_factory=list, description="user_profile memory result")
    semantic_memory: list[FragmentMemoryUnit] = Field(default_factory=list, description="semantic memory result")
    episodic_memory: list[FragmentMemoryUnit] = Field(default_factory=list, description="episodic memory result")
    summary: list[SummaryUnit] = Field(default_factory=list, description="summary result")


class LongTermMemory(metaclass=Singleton):
    """
        Abstract base class for memory engine.

        Defines the core interface for memory storage and retrieval operations.
        Provides unified memory management functionality including conversation memory,
        user variables, semantic search, and persistence.

        Concrete implementations should handle memory operations across multiple storage
        backends (KV store, semantic store, database store).
    """
    DEFAULT_VALUE: str = "__default__"
    SCOPE_CONFIG_KEY: str = "memory_scope_config"

    def __init__(self):
        """
        Initialize the memory engine
        """
        # config
        self._sys_mem_config: MemoryEngineConfig | None = None
        self._scope_config: dict[str, MemoryScopeConfig] = {}
        # store
        self.kv_store: BaseKVStore | None = None
        self.vector_store: BaseVectorStore | None = None
        self.db_store: BaseDbStore | None = None
        self.message_store: BaseMessageStore | None = None
        # memory index
        self.memory_index: BaseMemoryIndex | None = None
        self._storage_codec: StorageCodec | None = None
        self._index_backend: str = "simple"
        # managers
        self.scope_user_mapping_manager = None
        self.message_manager: MessageManager | None = None
        self.fragment_memory_manager = None
        self.variable_manager = None
        self.write_manager = None
        self.summary_manager = None
        self.search_manager = None
        self.generator = None
        self.fragment_type = None
        # llm
        self._base_llm: Model | None = None
        # embedding
        self._base_embed: Embedding | None = None
        # embedding model cache
        self._scope_embedding: dict[str, Embedding] = {}
        # dreaming: one orchestrator per (scope_id, user_id)
        self._dreaming_orchestrators: dict[tuple[str, str], DreamingOrchestrator] = {}
        # asynchronous mode
        self._stop_event = asyncio.Event()
        self._background_task: asyncio.Task | None = None
        self._middle_memory_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._agent_config = None  # Save configuration
        self._batch_concurrency_limit = 20

    @property
    def storage_codec(self) -> StorageCodec | None:
        return self._storage_codec

    @property
    def index_backend(self) -> str:
        return self._index_backend

    def configure_index_backend(self, backend: str) -> None:
        if backend not in ("simple", "file"):
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="index backend",
                error_msg=(
                    f"unsupported index_backend={backend!r}, "
                    f"expected 'simple' or 'file'"
                ),
            )
        self._index_backend = backend

    async def register_plugin(self, name: str, cls: type, params: dict[str, Any]):
        """
        Register BaseMemoryIndex plugin.

        Args:
            name: Plugin name, describing the plugin type (e.g., 'vector', 'semantic_index')
            cls: Plugin class, inheriting from BaseMemoryIndex
            params: Initialization parameters for the plugin class

        Example:
            await memory.register_plugin(
                name='semantic_index',
                cls=SimpleMemoryIndex,
                params={'kv_store': kv_store,
                        'vector_store': vector_store,
                        'embedding_model': embedding_model}
            )
        """
        # Instantiate plugin
        plugin_instance = cls(**params)
        # Set default index if not already set
        if self.memory_index is None:
            self.memory_index = plugin_instance

    async def register_store(self, kv_store: BaseKVStore,
                             vector_store: BaseVectorStore | None = None,
                             db_store: BaseDbStore | None = None,
                             embedding_model: Embedding | None = None,
                             message_store: BaseMessageStore | None = None,
                             *,
                             index_backend: str = "simple",
                             file_root_dir: str | None = None):
        """
        Register store instance.

        Args:
            kv_store: Key-value store for fast structured data access
            vector_store: Vector storage for vector-based similarity search.
                在 ``index_backend="file"`` 时，vector_store 不会作为
                memory_index 后端，但仍可被中间记忆 / dreaming 的 SemanticStore 使用。
            db_store: Database store for persistent data storage
            embedding_model: Embedding model for semantic search
            message_store: Optional message store for conversation history
            index_backend: 记忆索引后端类型，``"simple"``（缺省，KV+Vector）
                或 ``"file"``（markdown+SQLite）。决定 memory_index 注册哪个实现。
                ``"file"`` 时忽略 vector_store 对 memory_index 的影响，改注册
                FileMemoryIndex。
            file_root_dir: ``index_backend="file"`` 时 FileMemoryIndex 的数据根目录，
                必填。``"simple"`` 时忽略。
        """
        if kv_store is None:
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="kv store",
                error_msg="kv store is required, cannot be None",
            )

        if vector_store is not None and not isinstance(vector_store, BaseVectorStore):
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="vector store",
                error_msg="vector store must be instance of BaseVectorStore",
            )

        if db_store is not None and not isinstance(db_store, BaseDbStore):
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="db store",
                error_msg="db store must be instance of BaseDbStore",
            )

        if message_store is not None and not isinstance(message_store, BaseMessageStore):
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="message store",
                error_msg="message store must be instance of BaseMessageStore",
            )

        # index_backend 取值校验：避免拼写错误（如 "File"/"files"）静默退化到 simple
        if index_backend not in ("simple", "file"):
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type="index backend",
                error_msg=f"unsupported index_backend={index_backend!r}, expected 'simple' or 'file'",
            )

        self.kv_store = kv_store
        self.vector_store = vector_store
        self.db_store = db_store
        self._base_embed = embedding_model
        self.message_store = message_store
        self._index_backend = index_backend

        # Register the retrieve_history KV prefix so the KV migrator /
        # batch cleaner picks it up alongside user_var / session_var.
        # Idempotent — re-registering on repeat register_store calls is a no-op.
        kv_prefix_registry.register_current(RETRIEVE_HISTORY_PREFIX)

        # memory_index 后端选择：index_backend 决定注册哪个实现。
        # - "file"：注册 FileMemoryIndex（markdown+SQLite），vector_store 不作 memory_index，
        #   但仍保留供中间记忆 / dreaming 的 SemanticStore 使用。
        # - "simple"（缺省）：vector_store + kv_store 齐全时注册 SimpleMemoryIndex。
        if index_backend == "file":
            if not file_root_dir:
                raise build_error(
                    StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                    store_type="file index",
                    error_msg="file_root_dir is required when index_backend='file'",
                )
            # resolve 成绝对路径并验证可创建（更早暴露权限/路径问题，
            # 避免启动后才在写入时崩）
            from pathlib import Path
            try:
                resolved_root = str(Path(file_root_dir).expanduser().resolve())
                Path(resolved_root).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise build_error(
                    StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                    store_type="file index",
                    error_msg=f"file_root_dir {file_root_dir!r} cannot be created or accessed: {e}",
                ) from e
            from jiuwen_memory.foundation.store.index.file_index import FileMemoryIndex
            await self.register_plugin(
                name="file_index",
                cls=FileMemoryIndex,
                params={
                    "root_dir": resolved_root,
                    "embedding_model": self._base_embed,
                },
            )
            # 接线 watcher：file 后端的核心能力是外部编辑 .md 实时增量同步，
            # register_plugin 仅实例化 memory_index，watcher 需在此显式启动。
            # register_store 是 async，调用必然处于事件循环内（满足 start_watcher
            # 的 "loop running" 前置条件）；watchdog 未安装时 start 为 no-op，
            # 优雅降级到 search 时的惰性 _ensure_synced。
            if self.memory_index is not None and hasattr(self.memory_index, "start_watcher"):
                self.memory_index.start_watcher()
        elif self.vector_store and self.kv_store:
            await self.register_plugin(
                name='semantic_index',
                cls=SimpleMemoryIndex,
                params={'kv_store': self.kv_store,
                        'vector_store': self.vector_store,
                        'embedding_model': self._base_embed}
            )

        if self.db_store:
            await create_tables(self.db_store)

        # Create internal SqlMessageStore if not provided externally, so it can be migrated
        if not self.message_store and self.db_store:
            sql_db_store = SqlDbStore(self.db_store)
            self.message_store = SqlMessageStore(sql_db_store=sql_db_store)

        self.set_config(MemoryEngineConfig())

        await self._run_migration(
            migrate_func=run_kv_migrations,
            store=self.kv_store,
            store_type="kv store"
        )

        if self.vector_store:
            await self._run_migration(
                migrate_func=run_vector_migrations,
                store=self.vector_store,
                store_type="vector store"
            )

        if self.db_store:
            sql_db_store = SqlDbStore(self.db_store)
            await self._run_migration(
                migrate_func=run_sql_migrations,
                store=sql_db_store,
                store_type="db store"
            )
        if self.message_store:
            await self._run_migration(
                migrate_func=run_message_migrations,
                store=self.message_store,
                store_type="message store"
            )

    @staticmethod
    async def migrate_between_indices(source_index: BaseMemoryIndex,
                                      target_index: BaseMemoryIndex) -> None:
        """
        Migrate data from one BaseMemoryIndex to another.

        Copies all memory documents from source index to target index in batches.
        Source data is preserved after migration.

        Args:
            source_index: Source BaseMemoryIndex to migrate data from.
            target_index: Target BaseMemoryIndex to migrate data into.
        """
        scopes = await source_index.list_user_scopes()

        for user_id, scope_id in scopes:
            offset = 0
            batch_size = 100

            while True:
                documents = await source_index.list_memories(user_id, scope_id, offset, batch_size)
                if not documents:
                    break

                target_documents = [
                    MemoryDoc(
                        id=doc.id,
                        text=doc.text,
                        type=doc.type,
                        timestamp=doc.timestamp,
                        fields=doc.fields.copy(),
                        is_important=doc.is_important,
                        blacklisted=doc.blacklisted,
                    )
                    for doc in documents
                ]
                await target_index.add_memories(user_id, scope_id, target_documents)
                offset += batch_size

        memory_logger.info(
            "Cross-index migration completed",
            event_type=LogEventType.MEMORY_INIT,
            metadata={"scope_count": len(scopes)}
        )

    def set_config(self, config: MemoryEngineConfig):
        """
        Set configuration.

        Args:
            config: memory engine configuration parameters
        """
        if not self.kv_store or not self.db_store:
            raise build_error(
                StatusCode.MEMORY_SET_CONFIG_EXECUTION_ERROR,
                config_type="system",
                error_msg="kv store and db store must be registered before setting config",
            )
        if not self.memory_index:
            raise build_error(
                StatusCode.MEMORY_SET_CONFIG_EXECUTION_ERROR,
                config_type="system",
                error_msg="memory_index must be provided (via register_plugin or register_store)",
            )
        # file 后端使用中间记忆 / dreaming 时需要 vector_store（SemanticStore 依赖它）。
        # 若 file 部署未配 vector_store 却开了 enable_middle_memory，启动后 add_messages
        # 写中间记忆会崩——这里给 warning 提示，不强制改配置。
        if self._index_backend == "file" and config.enable_middle_memory and not self.vector_store:
            memory_logger.warning(
                "index_backend='file' with enable_middle_memory=True but no vector_store registered; "
                "middle-term memory writes will fail (long-term memory is unaffected). "
                "Configure VECTOR_STORE_TYPE or disable enable_middle_memory.",
                event_type=LogEventType.MEMORY_INIT,
            )
        self._sys_mem_config = config

        # Codec resolution: look up a pre-built instance by name in the
        # registry; if none registered under config.codec, fall back to
        # AesStorageCodec built from crypto_key. The registry stores
        # already-constructed instances, so multi-param codecs (HSM
        # cert/slot/pin) are constructed by the user at registration time —
        # the engine never calls a constructor, hence no hard-coded ``key=``.
        codec: StorageCodec
        registered = get_default_registry().get(config.codec) if config.codec else None
        if registered is not None:
            if not isinstance(registered, StorageCodec):
                raise build_error(
                    StatusCode.MEMORY_SET_CONFIG_EXECUTION_ERROR,
                    config_type="codec",
                    error_msg=(
                        f"codec '{config.codec}' is not a StorageCodec instance, "
                        f"got {type(registered).__name__}"
                    ),
                )
            codec = registered
        else:
            if config.codec:
                memory_logger.warning(
                    "No codec registered under name '%s'; falling back to "
                    "AesStorageCodec(crypto_key). Register via "
                    "register_storage_codec() before set_config. Available: %s",
                    config.codec,
                    get_default_registry().names(),
                    event_type=LogEventType.MEMORY_INIT,
                )
            codec = AesStorageCodec(config.crypto_key)
        if self.memory_index:
            if self._index_backend == "file":
                # file 后端取舍：默认明文可读（让 .md 人类可读），需要加密时显式
                # 表达意图。注入条件 = crypto_key 非空 OR 用户显式选了 config.codec
                # （第三方 codec 如 SM4/HSM 不依赖 crypto_key，密钥已在实例里）。
                # 仅当两者皆空才是真正的"明文可读"设计；若用户显式选了 codec 却
                # 走明文，会静默忽略加密意图——此处门控避免该陷阱。
                if config.crypto_key or config.codec:
                    self.memory_index.set_storage_codec(codec)
                else:
                    memory_logger.info(
                        "index_backend='file' with no crypto_key and no codec; "
                        "markdown stays plaintext.",
                        event_type=LogEventType.MEMORY_INIT,
                    )
            else:
                # 向量后端等仍用 AES 加密
                self.memory_index.set_storage_codec(codec)
        self._storage_codec = codec

        data_id_generator = DataIdManager()

        sql_db_store = SqlDbStore(self.db_store) if self.db_store else None
        if sql_db_store:
            self.scope_user_mapping_manager = ScopeUserMappingManager(sql_db_store)

        if self.message_store:
            if isinstance(self.message_store, SqlMessageStore) and self.message_store.crypto_key is None:
                self.message_store.crypto_key = config.crypto_key
            # Inject the engine-level codec into the message store via its
            # public method (replaces the previous isinstance + private
            # _codec assignment, which only worked for SqlMessageStore).
            self.message_store.set_codec(codec)
            self.message_manager = MessageManager(store=self.message_store)
        self.fragment_memory_manager = FragmentMemoryManager(
            memory_index=self.memory_index,
            crypto_key=config.crypto_key
        )
        self.summary_manager = SummaryManager(
            memory_index=self.memory_index,
            crypto_key=self._sys_mem_config.crypto_key
        )

        self.middle_mem_manager = MiddleTermMemoryManager(
            memory_index=self.memory_index,
            crypto_key=self._sys_mem_config.crypto_key
        )

        self.variable_manager = VariableManager(
            self.kv_store,
            config.crypto_key,
            codec=codec,
        )

        managers = {
            MemoryType.USER_PROFILE.value: self.fragment_memory_manager,
            MemoryType.EPISODIC_MEMORY.value: self.fragment_memory_manager,
            MemoryType.SEMANTIC_MEMORY.value: self.fragment_memory_manager,
            MemoryType.VARIABLE.value: self.variable_manager,
            MemoryType.SUMMARY.value: self.summary_manager
        }

        middle_managers = {MemoryType.MIDDLE_TERM_MEMORY.value: self.middle_mem_manager}
        self.fragment_type = [MemoryType.USER_PROFILE.value, MemoryType.EPISODIC_MEMORY.value,
                              MemoryType.SEMANTIC_MEMORY.value]
        self.write_manager = WriteManager(managers, self.memory_index, middle_manager=self.middle_mem_manager)
        self.middle_write_manager = WriteManager(middle_managers, self.memory_index)

        self.search_manager = SearchManager(
            managers,
            config.crypto_key,
            self.memory_index,
            kv_store=self.kv_store,
        )

        self.middle_search_manager = SearchManager(
            middle_managers,
            config.crypto_key,
            self.memory_index,
            kv_store=self.kv_store,
        )

        self.generator = Generator(data_id_generator=data_id_generator, search_manager=self.search_manager)
        # set init llm
        if config.default_model_cfg and config.default_model_client_cfg:
            llm = LongTermMemory._get_llm_from_config(model_config=config.default_model_cfg,
                                                    model_client_config=config.default_model_client_cfg)
            self._base_llm = llm

        self._enable_hierarchical_memory = config.enable_middle_memory
        self.middle_memory_check_interval = config.middle_memory_check_interval


    async def start(
        self,
        user_id: str = DEFAULT_VALUE,
        scope_id: str = DEFAULT_VALUE,
        agent_config: AgentMemoryConfig | None = None,
    ):
        if not self._enable_hierarchical_memory:
            return

        worker_key = (scope_id, user_id)
        existing_task = self._middle_memory_tasks.get(worker_key)
        if existing_task is not None and not existing_task.done():
            memory_logger.warning("Middle memory processor is already running")
            return

        if agent_config is None:
            agent_config = AgentMemoryConfig(
                mem_variables=[],
                enable_long_term_mem=True,
                enable_user_profile=True,
                enable_semantic_memory=True,
                enable_episodic_memory=True,
                enable_summary_memory=True,
            )

        self._stop_event.clear()

        task = asyncio.create_task(
            self._middle_memory_loop(agent_config, user_id=user_id, scope_id=scope_id),
            name=f"MiddleMemoryTask/{scope_id}/{user_id}",
        )
        self._middle_memory_tasks[worker_key] = task
        self._background_task = task

        memory_logger.info("Middle memory processor started successfully (asyncio task mode)")


    async def stop(self):
        """Stop background tasks (safe shutdown)"""
        if not self._enable_hierarchical_memory:
            return

        self._stop_event.set()

        tasks = [
            task
            for task in self._middle_memory_tasks.values()
            if not task.done()
        ]

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._middle_memory_tasks.clear()
        self._background_task = None
        memory_logger.info("Middle memory process stopped")

    async def _process_dialogue_batch_to_long_term_safe(
        self, dialogue_batch, agent_config, user_id, scope_id, session_id, timestamp_str, mem_ids
    ):
        """Safe batch processing version, for thread pool calls
        Each batch independently gets LLM and semantic_store, avoid concurrency conflicts
        """

        if not dialogue_batch:
            return {"success": False, "mem_ids": [], "error": "Empty batch"}

        try:
            memory_logger.info(f"[Thread] Processing batch with {len(dialogue_batch)} dialogues")
            # Each thread independently gets resources (avoid concurrency conflicts)
            llm = await self._get_scope_llm(scope_id)
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)
            scope_config = await self._get_scope_config(scope_id)

            if not llm:
                return {"success": False, "mem_ids": mem_ids, "error": "LLM not ready"}

            memory_logger.info(f"[Thread] LLM ready, parsing dialogues...")
            # Parse conversation
            converted = []
            for dialogue in dialogue_batch:
                try:
                    if dialogue.role == 'user':
                        converted.append(UserMessage(role="user", content=dialogue.content))
                    else:
                        converted.append(AssistantMessage(role="assistant", content=dialogue.content))
                except Exception as e:
                    memory_logger.warning(f"Failed to parse dialogue: {str(e)}")
                    continue

            if not converted:
                return {"success": False, "mem_ids": mem_ids, "error": "No valid messages"}
            memory_logger.info(f"[Thread] Parsed {len(converted)} messages, extracting memories...")
            check_res, valid_msgs = self._check_messages(converted)

            if not check_res:
                memory_logger.info(
                    "[Thread] Skipping batch without user messages",
                    scope_id=scope_id,
                    user_id=user_id,
                )
                return {
                    "success": True,
                    "mem_ids": mem_ids,
                    "batch_size": len(dialogue_batch),
                    "memories_count": 0,
                    "skipped": True,
                    "skip_reason": "No valid user messages",
                }

            memories = await self.generator.gen_all_memory(
                user_id=user_id,
                scope_id=scope_id,
                messages=valid_msgs,
                history_messages=[],
                session_id=session_id,
                config=agent_config,
                base_chat_model=llm,
                message_mem_id=mem_ids[-1][:-4] if mem_ids else "",
                timestamp=timestamp_str,
                forbidden_variables=self._sys_mem_config.forbidden_variables,
                summary_max_token=self._sys_mem_config.single_turn_history_summary_max_token,
                scope_config=scope_config,
                semantic_store=semantic_store,
            )

            # Store long-term memory (execute independently within thread)
            if memories:
                memory_logger.info(f"[Thread] Storing {len(memories)} long-term memories")
                await self.write_manager.add_memories(
                    user_id=user_id, scope_id=scope_id, memories=memories, llm=llm,
                    extract_assistant_memory=bool(getattr(scope_config, "extract_assistant_memory", False)),
                )
                # Return processing result (do not delete here, unified batch deletion)
                return {
                    "success": True,
                    "mem_ids": mem_ids,
                    "memories_count": len(memories) if memories else 0,
                    "batch_size": len(dialogue_batch),
                }

        except Exception as e:
            import traceback
            memory_logger.error(
                f"Batch processing exception: {str(e)}", exception=str(e), traceback=traceback.format_exc()
            )
            return {"success": False, "mem_ids": mem_ids, "error": str(e)}

    async def _batch_delete_middle_messages(self, mem_ids: list):
        """Batch delete middle-term memories (performance optimization)
        """
        if not mem_ids:
            return

        try:
            # Try batch deletion (if message_manager supports)
            if hasattr(self.message_manager, "delete_by_ids"):
                await self.message_manager.delete_by_ids(mem_ids)
                memory_logger.info(f"Batch deleted {len(mem_ids)} middle memories")
            else:
                # Fall back to concurrent deletion
                delete_tasks = [self.message_manager.delete_by_id(mem_id) for mem_id in mem_ids]
                await asyncio.gather(*delete_tasks, return_exceptions=True)
                memory_logger.info(f"Deleted {len(mem_ids)} middle memories")

        except Exception as e:
            memory_logger.error(f"Batch delete failed, falling back to sequential: {str(e)}", exception=str(e))
            # Delete one by one as fallback
            for mem_id in mem_ids:
                try:
                    await self.message_manager.delete_by_id(mem_id)
                except Exception as inner_e:
                    memory_logger.warning(f"Failed to delete middle memory {mem_id}: {str(inner_e)}")

    async def _batch_delete_middle_memories(self, mem_ids: list, user_id: str = DEFAULT_VALUE,
                                            scope_id: str = DEFAULT_VALUE):
        """Batch delete middle-term memories (performance optimization)

        Note: Middle-term memories are only stored in semantic_store (vector store),
        not in memory_index. So we need to call middle_mem_manager.delete() directly,
        bypassing WriteManager which would try to query memory_index first.
        """
        if not mem_ids:
            return

        try:
            # Get semantic store for deletion
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)

            # Batch delete from semantic store directly
            # Note: middle_mem_manager only stores in semantic_store, not in memory_index
            delete_tasks = [
                self.middle_mem_manager.delete(
                    user_id=user_id,
                    scope_id=scope_id,
                    mem_id=mem_id[:-4],
                    semantic_store=semantic_store
                )
                for mem_id in mem_ids
            ]
            await asyncio.gather(*delete_tasks, return_exceptions=True)
            memory_logger.info(f"Deleted {len(mem_ids)} middle memories from semantic store")

        except Exception as e:
            memory_logger.error(f"Batch delete failed: {str(e)}", exception=str(e))
            # Fallback: try message_manager if available
            if hasattr(self.message_manager, "delete_by_ids"):
                try:
                    await self.message_manager.delete_by_ids(mem_ids)
                    memory_logger.info(f"Batch deleted {len(mem_ids)} middle memories via middle_mem_manager")
                except Exception as inner_e:
                    memory_logger.warning(f"Fallback deletion also failed: {str(inner_e)}")


    async def _middle_memory_loop(self, agent_config: AgentMemoryConfig,
                                  user_id: str = DEFAULT_VALUE, scope_id: str = DEFAULT_VALUE):
        """Async background loop: automatically execute middle_mem_to_long"""
        memory_logger.info("Middle memory background loop started")

        # Wait for initialization to complete
        await asyncio.sleep(3)

        while not self._stop_event.is_set():
            try:
                memory_logger.info("=== Executing middle_mem_to_long ===")
                await self.middle_mem_to_long(
                    agent_config=agent_config, user_id=user_id, scope_id=scope_id
                )
                # Execute every 10 seconds for easy testing
                await asyncio.sleep(self.middle_memory_check_interval)

            except asyncio.CancelledError:
                memory_logger.info("Middle memory loop cancelled")
                break
            except Exception as e:
                import traceback
                memory_logger.error("Middle memory loop error", exception=str(e), traceback=traceback.format_exc())
                await asyncio.sleep(10)


    async def set_scope_config(self, scope_id: str, memory_scope_config: MemoryScopeConfig) -> bool:
        """
        Set the scope-specific memory configuration and store it in kv_store.

        Args:
            scope_id: The scope identifier.
            memory_scope_config: The scope-specific memory configuration.


        Returns:
            True if the configuration was set successfully, False otherwise.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_STORE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_STORE,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_SET_CONFIG_EXECUTION_ERROR,
                config_type="scope",
                error_msg="invalid scope_id format",
            )
        # Create a deep copy of the config to avoid modifying the original
        encrypted_config = copy.deepcopy(memory_scope_config)

        # Encrypt API keys if they exist
        if encrypted_config.model_client_cfg and encrypted_config.model_client_cfg.api_key:
            encrypted_config.model_client_cfg.api_key = self._storage_codec.encode(
                encrypted_config.model_client_cfg.api_key
            )

        if encrypted_config.embedding_cfg and encrypted_config.embedding_cfg.api_key:
            encrypted_config.embedding_cfg.api_key = self._storage_codec.encode(
                encrypted_config.embedding_cfg.api_key
            )

        self._scope_config[scope_id] = encrypted_config

        config_key = f"{self.SCOPE_CONFIG_KEY}/{scope_id}"
        config_json = encrypted_config.model_dump_json(by_alias=True)
        await self.kv_store.set(config_key, config_json)

        # Clear cached embedding model for this scope since configuration changed
        if scope_id in self._scope_embedding:
            del self._scope_embedding[scope_id]

        return True

    async def get_scope_config(self, scope_id: str) -> MemoryScopeConfig | None:
        """
        Get the scope-specific memory configuration from kv_store.

        Args:
            scope_id: Unique identifier for the scope

        Returns:
            MemoryScopeConfig: The decrypted memory configuration for the scope, or None if not found
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="scope_config",
                error_msg="invalid scope_id format",
            )
        config_key = f"{self.SCOPE_CONFIG_KEY}/{scope_id}"
        config_json = await self.kv_store.get(config_key)

        if not config_json:
            return None

        # Parse the JSON into MemoryScopeConfig
        encrypted_config = MemoryScopeConfig.model_validate_json(config_json)

        # Decrypt API keys if they exist
        if encrypted_config.model_client_cfg and encrypted_config.model_client_cfg.api_key:
            encrypted_config.model_client_cfg.api_key = self._storage_codec.decode(
                encrypted_config.model_client_cfg.api_key
            )

        if encrypted_config.embedding_cfg and encrypted_config.embedding_cfg.api_key:
            encrypted_config.embedding_cfg.api_key = self._storage_codec.decode(
                encrypted_config.embedding_cfg.api_key
            )

        return encrypted_config

    async def delete_scope_config(self, scope_id: str) -> bool:
        """
        Delete the scope-specific memory configuration from kv_store.

        Args:
            scope_id: The scope identifier whose configuration should be deleted.

        Returns:
            True if the configuration was deleted successfully, False otherwise.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_DELETE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_DELETE,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="scope_config",
                error_msg="invalid scope_id format",
            )
        try:
            config_key = f"{self.SCOPE_CONFIG_KEY}/{scope_id}"
            await self.kv_store.delete(config_key)

            if scope_id in self._scope_config:
                del self._scope_config[scope_id]

            if scope_id in self._scope_embedding:
                del self._scope_embedding[scope_id]

            memory_logger.debug(
                "Successfully deleted configuration.",
                event_type=LogEventType.MEMORY_DELETE,
                scope_id=scope_id
            )
            return True
        except Exception as e:
            memory_logger.error(
                "Failed to delete configuration.",
                event_type=LogEventType.MEMORY_DELETE,
                exception=str(e),
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="scope_config",
                error_msg=f"failed to delete scope config: {str(e)}",
                cause=e
            ) from e

    async def delete_mem_by_scope(self, scope_id: str) -> bool:
        """
        Delete all memories associated with a specific scope.

        Args:
            scope_id: The scope identifier whose memories should be deleted.

        Returns:
            True if all memories were deleted successfully, False otherwise.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_DELETE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_DELETE,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        scope_user_data = await self.scope_user_mapping_manager.get_by_scope_id(scope_id=scope_id) or []
        user_ids = list(dict.fromkeys(
            scope_user["user_id"]
            for scope_user in scope_user_data
            if scope_user.get("user_id")
        ))

        middle_semantic_store = None
        if self.middle_write_manager and self.vector_store:
            middle_semantic_store = await self._create_semantic_store_with_embedding(scope_id)

        for user_id in user_ids:
            worker_key = (scope_id, user_id)
            middle_task = self._middle_memory_tasks.pop(worker_key, None)
            if middle_task is not None and not middle_task.done():
                middle_task.cancel()
                await asyncio.gather(middle_task, return_exceptions=True)

            lock = DistributedLock(self.kv_store, f"user/{user_id}")
            async with lock:
                if self.write_manager:
                    await self.write_manager.delete_mem_by_user_id(
                        scope_id=scope_id,
                        user_id=user_id
                    )

                    await self._cleanup_forgetting_traces(
                        scope_id=scope_id, user_id=user_id,
                    )

                if self.middle_write_manager and middle_semantic_store:
                    await self.middle_write_manager.delete_mem_by_user_id(
                        scope_id=scope_id,
                        user_id=user_id,
                        semantic_store=middle_semantic_store,
                    )

                if self.message_manager:
                    await self.message_manager.delete_by_user_and_scope(
                        user_id=user_id,
                        scope_id=scope_id,
                    )
                    await self.message_manager.delete_by_user_and_scope(
                        user_id=user_id,
                        scope_id=f"middle_term_memory:{scope_id}:{user_id}",
                    )
        await self.scope_user_mapping_manager.delete_by_scope_id(scope_id=scope_id)
        memory_logger.debug(
            "Successfully deleted memories.",
            event_type=LogEventType.MEMORY_DELETE,
            scope_id=scope_id
        )
        return True

    async def check_dialogue_continuity(self, scope_id, previous_dialogue, current_dialogue):
        llm = await self._get_scope_llm(scope_id)
        result = await self.generator.check_continuity_analyzer(
            previous_dialogue=previous_dialogue, current_dialogue=current_dialogue, base_chat_model=llm
        )
        return result

    async def add_messages(
            self,
            messages: list[BaseMessage],
            agent_config: AgentMemoryConfig,
            *,
            user_id: str = DEFAULT_VALUE,
            scope_id: str = DEFAULT_VALUE,
            session_id: str = DEFAULT_VALUE,
            timestamp: datetime | None = None,
            gen_mem: bool = True,
            gen_mem_with_history_msg_num: int = 2,
    ) -> AddMemResult:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        if self._enable_hierarchical_memory:
            await self.start(user_id=user_id, scope_id=scope_id, agent_config=agent_config)
            if not self._validate_id(event_type=LogEventType.MEMORY_STORE, scope_id=scope_id):
                memory_logger.error(
                    "Invalid scope_id format.",
                    event_type=LogEventType.MEMORY_STORE,
                    scope_id=scope_id,
                    user_id=user_id
                )
                raise build_error(
                    StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                    memory_type="all",
                    error_msg="invalid scope_id format",
                )

            llm = await self._get_scope_llm(scope_id)
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)
            scope_config = await self._get_scope_config(scope_id)
            await self._apply_scope_embedding(scope_id)
            # user level distributed lock
            lock = DistributedLock(self.kv_store, f"user/{user_id}")
            async with lock:
                if not llm:
                    memory_logger.error(
                        "LLM is not initialized.",
                        event_type=LogEventType.MEMORY_STORE,
                        user_id=user_id,
                        scope_id=scope_id
                    )
                    raise build_error(
                        StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                        memory_type="all",
                        error_msg="LLM is not initialized",
                    )
                # add meta data
                await self.scope_user_mapping_manager.add(user_id=user_id, scope_id=scope_id)
                timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                # when multi messages, use last msg_id
                for i, msg in enumerate(messages):
                    msg_timestamp = timestamp + timedelta(milliseconds=i)
                    add_req = MessageAddRequest(
                        user_id=user_id,
                        scope_id=scope_id,
                        role=msg.role,
                        content=msg.content,
                        session_id=session_id,
                        timestamp=msg_timestamp
                    )
                    msg_id = await self.message_manager.add(add_req)

                    middle_term_unit = await self.generator.middle_term_memory_unit_generator(
                        user_id=user_id, msg=msg, message_mem_id=msg_id, timestamp=timestamp_str
                    )

                    await self.middle_write_manager.add_memories(
                        user_id=user_id,
                        scope_id=scope_id,
                        memories=middle_term_unit,
                        llm=llm,
                        semantic_store=semantic_store,
                    )

                    await self.message_manager.add(
                        MessageAddRequest(
                            user_id=user_id,
                            scope_id=f"middle_term_memory:{scope_id}:{user_id}",
                            role=msg.role,
                            content=msg.content,
                            session_id=session_id,
                            timestamp=msg_timestamp,
                        ),
                        msg_id=msg_id,
                    )
        else:
            if not self._validate_id(event_type=LogEventType.MEMORY_STORE, scope_id=scope_id):
                memory_logger.error(
                    "Invalid scope_id format.",
                    event_type=LogEventType.MEMORY_STORE,
                    scope_id=scope_id,
                    user_id=user_id
                )
                raise build_error(
                    StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                    memory_type="all",
                    error_msg="invalid scope_id format",
                )

            msg_id = "-1"
            llm = await self._get_scope_llm(scope_id)
            scope_config = await self._get_scope_config(scope_id)
            await self._apply_scope_embedding(scope_id)
            # user level distributed lock
            lock = DistributedLock(self.kv_store, f"user/{user_id}")
            async with lock:
                if not llm:
                    memory_logger.error(
                        "LLM is not initialized.",
                        event_type=LogEventType.MEMORY_STORE,
                        user_id=user_id,
                        scope_id=scope_id
                    )
                    raise build_error(
                        StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                        memory_type="all",
                        error_msg="LLM is not initialized",
                    )
                history_messages = await self._get_history_messages(
                    user_id=user_id,
                    scope_id=scope_id,
                    session_id=session_id,
                    history_window_size=gen_mem_with_history_msg_num)
                # add meta data
                await self.scope_user_mapping_manager.add(user_id=user_id, scope_id=scope_id)
                timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                # when multi messages, use last msg_id
                for i, msg in enumerate(messages):
                    msg_timestamp = timestamp + timedelta(milliseconds=i)
                    add_req = MessageAddRequest(
                        user_id=user_id,
                        scope_id=scope_id,
                        role=msg.role,
                        content=msg.content,
                        session_id=session_id,
                        timestamp=msg_timestamp
                    )
                    msg_id = await self.message_manager.add(add_req)

                if not gen_mem:
                    return AddMemResult()

                check_res, messages = self._check_messages(messages=messages)
                if not check_res:
                    memory_logger.debug(
                        "Memory engine no need to process messages.",
                        event_type=LogEventType.MEMORY_STORE,
                        memory_type="message",
                        memory_count=len(messages),
                        user_id=user_id,
                        scope_id=scope_id
                    )
                    return AddMemResult()

                all_memory = await self.generator.gen_all_memory(
                    scope_id=scope_id,
                    user_id=user_id,
                    messages=messages,
                    history_messages=history_messages,
                    session_id=session_id,
                    config=agent_config,
                    base_chat_model=llm,
                    message_mem_id=msg_id,
                    timestamp=timestamp_str,
                    forbidden_variables=self._sys_mem_config.forbidden_variables,
                    summary_max_token=self._sys_mem_config.single_turn_history_summary_max_token,
                    scope_config=scope_config
                )
                try:
                    write_result = await self.write_manager.add_memories(
                        user_id=user_id,
                        scope_id=scope_id,
                        memories=all_memory,
                        llm=llm,
                        extract_assistant_memory=bool(getattr(scope_config, "extract_assistant_memory", False)),
                    )
                    memory_logger.debug(
                        "Successfully added memory units.",
                        event_type=LogEventType.MEMORY_STORE,
                        memory_count=len(all_memory),
                        memory_type="all type",
                        user_id=user_id,
                        scope_id=scope_id
                    )
                except ValueError as e:
                    memory_logger.error(
                        "Failed to add mem.",
                        memory_type="unknown",
                        event_type=LogEventType.MEMORY_STORE,
                        exception=str(e),
                        user_id=user_id,
                        scope_id=scope_id
                    )
                    raise build_error(
                        StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                        memory_type="unknown",
                        error_msg=f"{str(e)}",
                        cause=e
                    ) from e
            return AddMemResult(
                variables=[var for var in write_result if var.mem_type.value == MemoryType.VARIABLE.value],
                user_profile=[var for var in write_result if var.mem_type.value == MemoryType.USER_PROFILE.value],
                semantic_memory=[var for var in write_result if var.mem_type.value == MemoryType.SEMANTIC_MEMORY.value],
                episodic_memory=[var for var in write_result if var.mem_type.value == MemoryType.EPISODIC_MEMORY.value],
                summary=[var for var in write_result if var.mem_type.value == MemoryType.SUMMARY.value]
            )

    async def middle_mem_to_long(
        self,
        agent_config: AgentMemoryConfig,
        user_id: str = DEFAULT_VALUE,
        scope_id: str = DEFAULT_VALUE,
        session_id: str = DEFAULT_VALUE,
    ):
        """[Core] Middle-term memory -> Long-term memory (thread pool concurrent version)
        """
        try:
            # Step 1: Get middle-term memories
            middle_messages_all = await self.message_manager.get(user_id=user_id,
                                                                 scope_id=f"middle_term_memory:{scope_id}:{user_id}",
                                                                 message_len=100)

            if not middle_messages_all:
                memory_logger.info("No middle memories to process")
                return

            memory_logger.info(f"Processing {len(middle_messages_all)} middle memories")

            # Step 2: Continuity check + batch partitioning (keep serial)
            batches = []
            dialogue_batch = []
            pre_dialogue = ""
            batch_mem_ids = []
            pre_timestamp = ""

            for each in middle_messages_all:
                cur_dialogue = each[0]
                cur_timestamp = each[1] if len(each) > 1 else ""
                cur_mem_id = each[2] if len(each) > 2 else ""

                # Add first one directly
                if not pre_dialogue:
                    dialogue_batch.append(cur_dialogue)
                    pre_dialogue = cur_dialogue
                    pre_timestamp = cur_timestamp
                    if cur_mem_id:
                        batch_mem_ids.append(cur_mem_id)
                    continue

                # Continuity check (serial)
                if pre_dialogue:
                    continuity_results = await self.check_dialogue_continuity(
                        scope_id=scope_id, previous_dialogue=pre_dialogue.content, current_dialogue=cur_dialogue.content
                    )
                else:
                    continuity_results = "true"

                if continuity_results == "true" and len(dialogue_batch) <= 10:
                    # Continuous: add to current batch
                    dialogue_batch.append(cur_dialogue)
                    pre_dialogue = cur_dialogue
                    pre_timestamp = cur_timestamp
                    if cur_mem_id:
                        batch_mem_ids.append(cur_mem_id)
                else:
                    # Not continuous: save current batch, start new batch
                    if dialogue_batch:
                        batches.append(
                            {
                                "dialogues": dialogue_batch.copy(),
                                "mem_ids": batch_mem_ids.copy(),
                                "timestamp": pre_timestamp,
                            }
                        )

                    # Reset
                    dialogue_batch = [cur_dialogue]
                    pre_dialogue = cur_dialogue
                    pre_timestamp = cur_timestamp
                    batch_mem_ids = [cur_mem_id] if cur_mem_id else []

            # Process the last batch
            if dialogue_batch:
                batches.append(
                    {"dialogues": dialogue_batch.copy(), "mem_ids": batch_mem_ids.copy(), "timestamp": pre_timestamp}
                )

            memory_logger.info(
                f"Split {len(middle_messages_all)} memories into {len(batches)} batches for concurrent processing"
            )

            # Step 3: Thread pool concurrent processing of all batches
            if not batches:
                return

            semaphore = asyncio.Semaphore(self._batch_concurrency_limit)
            tasks = []

            async def _run_batch_with_semaphore(batch_data):
                async with semaphore:
                    return await self._process_dialogue_batch_to_long_term_safe(
                        dialogue_batch=batch_data["dialogues"],
                        agent_config=agent_config,
                        user_id=user_id,
                        scope_id=scope_id,
                        session_id=session_id,
                        timestamp_str=batch_data["timestamp"],
                        mem_ids=batch_data["mem_ids"],
                    )

            for i, batch_data in enumerate(batches):
                task = asyncio.create_task(_run_batch_with_semaphore(batch_data))
                tasks.append(task)
                memory_logger.info(f"Batch {i+1} scheduled (size: {len(batch_data['dialogues'])}")


            # Step 4: Wait for all batches to complete
            memory_logger.info(f"Waiting for {len(tasks)} batch processing tasks to complete...")
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Step 5: Aggregate results + batch deletion
            all_mem_ids_to_delete = []
            successful_batches = 0
            failed_batches = 0

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    memory_logger.error(f"Batch {i + 1} failed with exception: {str(result)}", exception=str(result))
                    failed_batches += 1
                elif result and result.get("success"):
                    successful_batches += 1
                    all_mem_ids_to_delete.extend(result.get("mem_ids", []))
                    memory_logger.info(
                        f"Batch {i + 1} succeeded: processed {result.get('batch_size')} dialogues, "
                        f"extracted {result.get('memories_count', 0)} memories"
                    )
                else:
                    failed_batches += 1
                    error_msg = result.get("error", "Unknown error") if result else "No result"
                    memory_logger.warning(f"Batch {i + 1} failed: {error_msg}")

            # Step 6: Batch delete processed middle-term memories
            if all_mem_ids_to_delete:
                memory_logger.info(f"all_mem_ids_to_delete: {all_mem_ids_to_delete}")

                try:
                    await self._batch_delete_middle_memories(
                                all_mem_ids_to_delete,
                                user_id=user_id,
                                scope_id=scope_id,
                                )
                    await self._batch_delete_middle_messages(all_mem_ids_to_delete)
                except Exception as e:
                    memory_logger.error(f"Failed to batch delete middle memories: {str(e)}", exception=str(e))

            # Step 7: Output statistics
            memory_logger.info(
                f"Middle memory conversion completed: {successful_batches} succeeded, "
                f"{failed_batches} failed, {len(all_mem_ids_to_delete)} memories deleted"
            )

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            memory_logger.warning("middle_mem_to_long failed", exception=str(e), traceback=traceback.format_exc())

    async def get_recent_messages(
            self,
            user_id: str = DEFAULT_VALUE,
            scope_id: str = DEFAULT_VALUE,
            session_id: str = DEFAULT_VALUE,
            num: int = 10
    ) -> list[BaseMessage]:
        """
        Get recent messages.

        Args:
            user_id: Unique identifier for the user
            scope_id: Unique identifier for the scope
            session_id: Optional session identifier for scoping related messages
            num: message num

        Returns:
            Message list in order of writing.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                memory_type="message"
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg="invalid scope_id format",
            )
        recent_messages_tuple = await self.message_manager.get(
            user_id=user_id,
            scope_id=scope_id,
            session_id=session_id,
            message_len=num
        )
        recent_messages = [msg for msg, *_ in recent_messages_tuple]
        return recent_messages

    async def get_message_by_id(self, msg_id: str) -> Tuple[BaseMessage, datetime] | None:
        """
        Retrieve a specific message by its unique identifier.

        Args:
            msg_id: Unique identifier of the message to retrieve

        Returns:
            Tuple of (message object, creation timestamp)
        """
        if not self.message_manager:
            memory_logger.warning(
                "Message manager is not initialized.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                memory_type="message",
                memory_id=[msg_id]
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg="message manager is not initialized",
            )
        return await self.message_manager.get_by_id(msg_id)

    async def delete_messages_by_user_and_scope(
        self,
        user_id: str = DEFAULT_VALUE,
        scope_id: str = DEFAULT_VALUE,
    ):
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                memory_type="message"
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="message",
                error_msg="invalid scope_id format",
            )
        await self.message_manager.delete_by_user_and_scope(
            user_id=user_id,
            scope_id=scope_id
        )
        # the dreaming source is these messages; once they are gone the swept-session
        # checkpoint is stale (a reused session_id would otherwise be skipped forever).
        if self.kv_store:
            await self.kv_store.delete(f"dreaming/checkpoint/{scope_id}/{user_id}")

    async def delete_mem_by_id(self,
                               mem_id: str,
                               user_id: str = DEFAULT_VALUE,
                               scope_id: str = DEFAULT_VALUE):
        """
        Delete a specific memory by ID.

        Args:
            user_id: Unique identifier for the user
            scope_id: Unique identifier for the scope
            mem_id: Unique identifier of the memory to delete
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_DELETE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_DELETE,
                user_id=user_id,
                scope_id=scope_id,
                memory_id=[mem_id]
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        lock = DistributedLock(self.kv_store, f"user/{user_id}")
        semantic_store = await self._create_semantic_store_with_embedding(scope_id)
        async with lock:
            if not self.write_manager:
                raise build_error(
                    StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                    memory_type="all",
                    error_msg=f"write manager is not initialized",
                )
            await self.write_manager.delete_mem_by_id(
                user_id=user_id,
                scope_id=scope_id,
                mem_id=mem_id,
                semantic_store=semantic_store
            )
            await self._cleanup_forgetting_traces(
                scope_id=scope_id, user_id=user_id, mem_id=mem_id,
            )

    async def _cleanup_forgetting_traces(
        self, *, scope_id: str, user_id: str,
        mem_id: Optional[str] = None,
    ) -> None:
        """Drop retrieve_history KV traces for a delete operation.

        - ``mem_id`` given  → drop the single ``retrieve_history/{user}/
          {scope}/{mem_id}`` key (per-mem delete path).
        - ``mem_id`` is None → drop every ``retrieve_history/{user}/`` key
          via ``delete_by_prefix`` (batch delete path; covers all scopes
          for that user).

        Called from delete_mem_by_id / delete_mem_by_user_id /
        delete_mem_by_scope after the doc(s) are removed from the store.
        All errors are logged and swallowed — the main delete flow already
        succeeded; this is housekeeping.
        """
        if not self.kv_store:
            return
        if mem_id:
            # Per-mem: exact key drop.
            rh_key = self._retrieve_history_key(user_id, scope_id, mem_id)
            try:
                await self.kv_store.delete(rh_key)
            except Exception as exc:
                memory_logger.warning(
                    "cleanup_forgetting_traces: delete retrieve_history failed: %s",
                    str(exc),
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=user_id, scope_id=scope_id, mem_id=mem_id,
                )
        else:
            # Batch: prefix sweep over the user's retrieve_history keys.
            rh_prefix = (
                f"{RETRIEVE_HISTORY_PREFIX}{VariableManager.SEPARATOR}"
                f"{user_id}{VariableManager.SEPARATOR}"
            )
            try:
                await self.kv_store.delete_by_prefix(rh_prefix)
            except Exception as exc:
                memory_logger.warning(
                    "cleanup_forgetting_traces: delete retrieve_history prefix failed: %s",
                    str(exc),
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=user_id, scope_id=scope_id,
                )

    # ---------------------------------------------------------------- recall path

    async def add_memory_unit(
        self,
        *,
        scope_id: str,
        user_id: str,
        memory_doc: MemoryDoc,
        retrieve_history: dict | None = None,
    ) -> str:
        """
        Write a single ``MemoryDoc`` directly to the memory index.

        Used by the recall flow: the caller constructs the full doc (an
        evicted memory they want to "remember again"), and this method
        bypasses LLM extraction / conflict detection, writes straight to
        ``memory_index.add_memories`` with ``blacklisted=False`` (the
        default on ``MemoryDoc``), and optionally initializes a
        ``retrieve_history`` KV entry so the new unit starts with a
        non-zero reinforcement score.

        Args:
            scope_id: tenant / scope boundary.
            user_id: user identifier within the scope.
            memory_doc: fully-constructed doc (caller sets id / text /
                type / timestamp / fields / is_important). ``blacklisted``
                on the doc is ignored — this method always writes
                ``blacklisted=False`` (recall = un-forget).
            retrieve_history: optional initial payload to seed the
                retrieve_history KV (e.g. carry over the old count +
                timestamps). When None, no retrieve_history key is written.

        Returns:
            The written memory id (``memory_doc.id``).

        Raises:
            BaseError(StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR): on
                validation or write failures.
        """
        if not scope_id or not user_id:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR,
                error_msg="scope_id and user_id are required",
            )
        if memory_doc is None or not getattr(memory_doc, "id", ""):
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR,
                error_msg="memory_doc.id is required",
            )
        if not getattr(memory_doc, "text", ""):
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR,
                error_msg="memory_doc.text is required",
            )

        if not self.memory_index:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR,
                error_msg="memory index not initialized",
            )

        # Recall = un-forget. The caller's blacklisted flag is overwritten
        # to False; any stale blacklist entries are cleaned up by the
        # caller via delete_mem_by_id before re-adding.
        memory_doc.blacklisted = False

        try:
            await self.memory_index.add_memories(user_id, scope_id, [memory_doc])
        except BaseError:
            raise
        except Exception as exc:
            memory_logger.error(
                "add_memory_unit failed: %s", str(exc),
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id, scope_id=scope_id, mem_id=memory_doc.id,
            )
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_UNIT_ERROR,
                error_msg=str(exc),
            ) from exc

        # Optional retrieve_history seed. Failures here are swallowed —
        # the doc is already written; missing retrieve_history just means
        # the new memory scores like a fresh memory next forget sweep.
        if retrieve_history is not None and self.kv_store:
            rh_key = self._retrieve_history_key(user_id, scope_id, memory_doc.id)
            try:
                await self.kv_store.set(rh_key, json.dumps(retrieve_history))
            except Exception as exc:
                memory_logger.warning(
                    "add_memory_unit: retrieve_history seed failed, skip: %s",
                    str(exc),
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=user_id, scope_id=scope_id, mem_id=memory_doc.id,
                )

        return memory_doc.id

    async def delete_mem_by_user_id(self,
                                    user_id: str = DEFAULT_VALUE,
                                    scope_id: str = DEFAULT_VALUE):
        """
        Delete all type memories for a user with scope id.

        Useful for implementing "forget me" functionality or cleaning up user data.

        Args:
            user_id: User identifier whose memories should be deleted
            scope_id: Unique identifier for the scope
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_DELETE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_DELETE,
                user_id=user_id,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        lock = DistributedLock(self.kv_store, f"user/{user_id}")
        async with lock:
            if not self.write_manager:
                raise build_error(
                    StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                    memory_type="all",
                    error_msg=f"write manager is not initialized",
                )
            await self.write_manager.delete_mem_by_user_id(
                user_id=user_id,
                scope_id=scope_id
            )
            await self._cleanup_forgetting_traces(
                scope_id=scope_id, user_id=user_id,
            )

    async def update_mem_by_id(self,
                               mem_id: str,
                               memory: str,
                               user_id: str = DEFAULT_VALUE,
                               scope_id: str = DEFAULT_VALUE):
        """
        Update the content of an existing memory entry.

        Args:
            mem_id: Unique identifier of the memory to update
            memory: New content for the memory
            user_id: Unique identifier for the user
            scope_id: Unique identifier for the scope
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_UPDATE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format",
                event_type=LogEventType.MEMORY_UPDATE,
                user_id=user_id,
                scope_id=scope_id,
                memory_id=[mem_id]
            )
            raise build_error(
                StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        lock = DistributedLock(self.kv_store, f"user/{user_id}")
        async with lock:
            if not self.write_manager:
                raise build_error(
                    StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                    memory_type="all",
                    error_msg=f"write manager is not initialized",
            )
            await self._apply_scope_embedding(scope_id)
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)
            updated = await self.write_manager.update_mem_by_id(
                user_id=user_id,
                scope_id=scope_id,
                mem_id=mem_id,
                memory=memory,
                semantic_store=semantic_store,
            )
            if updated:
                return

            raise build_error(
                StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"memory {mem_id} not found",
            )

    async def get_variables(self,
                            names: list[str] | str | None = None,
                            user_id: str = DEFAULT_VALUE,
                            scope_id: str = DEFAULT_VALUE) -> dict[str, str]:
        """
            Get user variable(s)

            Args:
                names: Name of the variable(s) to get.
                       - None: return all variables
                       - str: return one variable
                       - list[str]: return multiple variables
                user_id: user identifier
                scope_id: scope identifier

            Returns:
                dict[str, str]: variable name -> value
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                memory_type=MemoryType.VARIABLE.value,
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.VARIABLE.value,
                error_msg="invalid scope_id format",
            )
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )
        ret: dict[str, str] = {}
        if names is None:
            return await self.search_manager.get_all_user_variable(user_id=user_id, scope_id=scope_id)
        if isinstance(names, str):
            value = await self.search_manager.get_user_variable(user_id, scope_id, names)
            ret[names] = value
            return ret
        if isinstance(names, list):
            for name in names:
                value = await self.search_manager.get_user_variable(user_id, scope_id, name)
                ret[name] = value
            return ret
        raise build_error(
            StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
            memory_type="all",
            error_msg=f"names must be str | list[str] | None",
        )

    @staticmethod
    def _retrieve_history_key(user_id: str, scope_id: str, mem_id: str) -> str:
        """retrieve_history KV key. Layout matches SearchManager.RETRIEVE_HISTORY_PREFIX.

        Co-located with the cleanup / add_memory_unit paths so they can
        format the same key without going through SearchManager (which
        is the fire-and-forget writer for the search path).
        """
        sep = VariableManager.SEPARATOR
        return f"{RETRIEVE_HISTORY_PREFIX}{sep}{user_id}{sep}{scope_id}{sep}{mem_id}"

    async def search_user_mem(self,
                              query: str,
                              num: int,
                              user_id: str = DEFAULT_VALUE,
                              scope_id: str = DEFAULT_VALUE,
                              threshold: float = 0.3,
                              *,
                              filters: "FilterGroup | None" = None,
                              ) -> list[MemResult]:
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                query=query,
                user_id=user_id,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg="invalid scope_id format",
            )
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )
        await self._apply_scope_embedding(scope_id)
        params = SearchParams(
            query=query,
            scope_id=scope_id,
            top_k=num,
            user_id=user_id,
            threshold=threshold,
            search_type=self.fragment_type,
            filters=filters,
        )
        try:
            search_data = []
            search_data = await self.search_manager.search(params)
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)

            search_data = sorted(search_data, key=lambda x: x.get("score", 0.0), reverse=True)[:num]
            mem_results: list[MemResult] = [
                MemResult(
                    mem_info=MemInfo(
                        mem_id=item["id"],
                        content=item["mem"],
                        type=item.get("mem_type", None),
                        timestamp=item.get("timestamp")
                    ),
                    score=item.get("score", 0.0)
                )
                for item in search_data
            ]

            params.search_type = None

            res_middle = await self.middle_search_manager.search_middle(params, semantic_store)

            middle_results = [
                MemResult(
                    mem_info=MemInfo(
                        mem_id=middle_memory[0],
                        content=middle_memory[2],
                        type="middle_term_memory",
                        # timestamp=middle_memory[3],
                    ),
                    score=middle_memory[1],
                )
                for middle_memory in res_middle
            ]
            mem_results.extend(middle_results)

            # retrieve_history is now appended inside SearchManager.search
            # (and middle_search_manager.search_middle does not, by design —
            # middle-term memories are not subject to Ebbinghaus forgetting).
            return mem_results

        except AttributeError as e:
            memory_logger.debug(
                "Search user mem has attribute exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                user_id=user_id,
                scope_id=scope_id,
                query=query,
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e
        except ValueError as e:
            memory_logger.warning(
                "Search user mem has value exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e
        except Exception as e:
            memory_logger.warning(
                "Search user mem has exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e

    async def search_middle_mem(self,
                              query: str,
                              num: int,
                              user_id: str = DEFAULT_VALUE,
                              scope_id: str = DEFAULT_VALUE,
                              threshold: float = 0.3
                              ):
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                query=query,
                user_id=user_id,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg="invalid scope_id format",
            )
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )
        await self._apply_scope_embedding(scope_id)
        semantic_store = await self._create_semantic_store_with_embedding(scope_id)
        params = SearchParams(
            query=query,
            scope_id=scope_id,
            top_k=num,
            user_id=user_id,
            threshold=threshold,
            search_type=self.fragment_type
        )
        try:
            params.search_type = None
            res_middle = await self.middle_search_manager.search_middle(params, semantic_store)

            return res_middle

        except AttributeError as e:
            memory_logger.debug(
                "Search user mem has attribute exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                user_id=user_id,
                scope_id=scope_id,
                query=query,
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e
        except ValueError as e:
            memory_logger.warning(
                "Search user mem has value exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e
        except Exception as e:
            memory_logger.warning(
                "Search user mem has exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="user_mem",
                error_msg=str(e),
                cause=e
            ) from e

    async def search_user_history_summary(
            self,
            query: str,
            num: int,
            user_id: str = DEFAULT_VALUE,
            scope_id: str = DEFAULT_VALUE,
            threshold: float = 0.3
    ) -> list[MemResult]:
        """
        Search user summary.

        Args:
            query: Search query string
            num: Number of results to return
            user_id: user identifier
            scope_id: scope identifier
            threshold: Minimum similarity threshold for results

        Returns:
            List of memory information
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                query=query,
                memory_type=MemoryType.SUMMARY.value,
                user_id=user_id,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="history_summary",
                error_msg="invalid scope_id format",
            )
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )
        await self._apply_scope_embedding(scope_id)
        params = SearchParams(
            query=query,
            scope_id=scope_id,
            top_k=num,
            user_id=user_id,
            threshold=threshold,
            search_type=[MemoryType.SUMMARY.value]
        )
        try:
            search_data = await self.search_manager.search(params)
            mem_results: list[MemResult] = [
                MemResult(
                    mem_info=MemInfo(
                        mem_id=item["id"],
                        content=item["mem"],
                        type=item.get("mem_type", MemoryType.SUMMARY),
                        timestamp=item.get("timestamp")
                    ),
                    score=item.get("score", 0.0)
                )
                for item in search_data
            ]
            return mem_results
        except AttributeError as e:
            memory_logger.debug(
                "Search user history summary has attribute exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                user_id=user_id,
                scope_id=scope_id,
                query=query,
                memory_type=MemoryType.SUMMARY.value
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="history_summary",
                error_msg=str(e),
                cause=e
            ) from e
        except ValueError as e:
            memory_logger.warning(
                "Search user history summary has value exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                memory_type=MemoryType.SUMMARY.value,
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="history_summary",
                error_msg=str(e),
                cause=e
            ) from e
        except Exception as e:
            memory_logger.warning(
                "Search user history summary has exception.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,
                exception=str(e),
                memory_type=MemoryType.SUMMARY.value,
                query=query
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="history_summary",
                error_msg=str(e),
                cause=e
            ) from e

    async def user_mem_total_num(self,
                                 user_id: str = DEFAULT_VALUE,
                                 scope_id: str = DEFAULT_VALUE) -> int:
        """
        return total number of user memory
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id
            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        # Get all user profiles by using get_in_range with a large range
        search_data = await self.search_manager.list_user_profile(user_id=user_id,
                                                                  scope_id=scope_id)
        return len(search_data)

    async def get_user_mem_by_page(self,
                                   user_id: str = DEFAULT_VALUE,
                                   scope_id: str = DEFAULT_VALUE,
                                   page_size: int = 10,
                                   page_idx: int = 1,
                                   memory_type: MemoryType = MemoryType.UNKNOWN,
                                   *,
                                   filters: "FilterGroup | None" = None) -> list[MemInfo]:
        """
        List user memories with pagination support.

        Retrieves memories in chronological order, suitable for displaying
        conversation history or memory browsing interfaces.

        Args:
            user_id: User identifier to search within
            scope_id: Unique identifier for the scope
            page_size: Number of memories per page
            page_idx: Page index (1-based)
            memory_type: Memory type to filter. If UNKNOWN, no filtering is applied.
            filters: caller-supplied FilterGroup. None (default) = auto-inject
                ``NE("blacklisted", True)`` — the normal user-facing listing
                excluding forgotten memories. Callers wanting to surface
                blacklisted memories pass a FilterGroup that mentions the
                ``blacklisted`` field (e.g. ``EQ("blacklisted", True)``).

        Returns:
            List of memory information
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_RETRIEVE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                user_id=user_id,
                scope_id=scope_id,

            )
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg="invalid scope_id format",
            )
        if memory_type == MemoryType.MIDDLE_TERM_MEMORY:
            return await self._list_middle_memories_by_page(
                user_id=user_id,
                scope_id=scope_id,
                page_size=page_size,
                page_idx=page_idx,
            )
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )
        if memory_type != MemoryType.UNKNOWN:
            search_data = await self.search_manager.list_user_mem(user_id=user_id, scope_id=scope_id,
                                                                  nums=page_size, pages=page_idx,
                                                                  mem_type=memory_type.value,
                                                                  filters=filters)
            return self._build_mem_info_list(search_data)

        start_idx = page_size * (page_idx - 1)
        fetch_size = start_idx + page_size
        search_data = await self.search_manager.list_user_mem(user_id=user_id, scope_id=scope_id,
                                                              nums=fetch_size, pages=1,
                                                              mem_type=None,
                                                              filters=filters)
        mem_results = self._build_mem_info_list(search_data)
        mem_results.extend(await self._list_middle_memories(user_id=user_id, scope_id=scope_id,
                                                            limit=fetch_size))
        mem_results.sort(key=self._mem_info_timestamp_sort_key)
        return mem_results[start_idx:start_idx + page_size]

    @staticmethod
    def _build_mem_info_list(search_data: list[dict] | None) -> list[MemInfo]:
        if not search_data:
            return []
        mem_results: list[MemInfo] = []
        for item in search_data:
            mem_type = item.get("mem_type", MemoryType.UNKNOWN.value)
            mem_results.append(
                MemInfo(
                    mem_id=item["id"],
                    content=item["mem"],
                    type=mem_type,
                    timestamp=item.get("timestamp")
                )
            )
        return mem_results

    @staticmethod
    def _mem_info_timestamp_sort_key(mem_info: MemInfo) -> float:
        if mem_info.timestamp is None:
            return 0.0
        if mem_info.timestamp.tzinfo is None:
            return mem_info.timestamp.replace(tzinfo=timezone.utc).timestamp()
        return mem_info.timestamp.timestamp()

    async def _list_middle_memories_by_page(
            self,
            user_id: str,
            scope_id: str,
            page_size: int,
            page_idx: int,
    ) -> list[MemInfo]:
        start_idx = page_size * (page_idx - 1)
        fetch_size = start_idx + page_size
        mem_results = await self._list_middle_memories(user_id=user_id, scope_id=scope_id, limit=fetch_size)
        return mem_results[start_idx:start_idx + page_size]

    async def _list_middle_memories(self, user_id: str, scope_id: str, limit: int) -> list[MemInfo]:
        if limit <= 0 or not self.message_manager:
            return []
        middle_scope_id = f"middle_term_memory:{scope_id}:{user_id}"
        middle_messages = await self.message_manager.get(
            user_id=user_id,
            scope_id=middle_scope_id,
            message_len=limit,
        )
        return [
            MemInfo(
                mem_id=msg_id,
                content=message.content,
                type=MemoryType.MIDDLE_TERM_MEMORY,
                timestamp=timestamp,
            )
            for message, timestamp, msg_id in middle_messages
        ]

    async def update_variables(self,
                                   variables: dict[str, str],
                                   user_id: str = DEFAULT_VALUE,
                                   scope_id: str = DEFAULT_VALUE
                                   ):
        """
        Update user variables.

        Args:
            variables: variable name to value pairs
            user_id: User identifier to search within
            scope_id: Unique identifier for the scope
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_UPDATE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_UPDATE,
                user_id=user_id,
                scope_id=scope_id,
                memory_type=MemoryType.VARIABLE.value
            )
            raise build_error(
                StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                memory_type="variable",
                error_msg="invalid scope_id format",
            )
        lock = DistributedLock(self.kv_store, f"user/{user_id}")
        async with lock:
            if not self.variable_manager:
                raise build_error(
                    StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                    memory_type="variable",
                    error_msg=f"variable manager is not initialized",
                )
            for name, value in variables.items():
                await self.variable_manager.update_user_variable(
                    user_id=user_id,
                    scope_id=scope_id,
                    var_name=name,
                    var_mem=value
                )

    async def delete_variables(self,
                                   names: list[str],
                                   user_id: str = DEFAULT_VALUE,
                                   scope_id: str = DEFAULT_VALUE):
        """
        Delete user variables.

        Args:
            names: Name of the variables to delete
            user_id: User identifier to search within
            scope_id: Unique identifier for the scope
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_DELETE, scope_id=scope_id):
            memory_logger.error(
                "Invalid scope_id format.",
                event_type=LogEventType.MEMORY_DELETE,
                user_id=user_id,
                scope_id=scope_id,
                memory_type=MemoryType.VARIABLE.value
            )
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type="variable",
                error_msg="invalid scope_id format",
            )
        lock = DistributedLock(self.kv_store, f"user/{user_id}")
        async with lock:
            if not self.variable_manager:
                raise build_error(
                    StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                    memory_type="variable",
                    error_msg=f"variable manager is not initialized",
                )
            for name in names:
                await self.variable_manager.delete_user_variable(user_id=user_id, scope_id=scope_id, var_name=name)
            return True

    @staticmethod
    def _get_llm_from_config(model_config: ModelRequestConfig,
                             model_client_config: ModelClientConfig):
        return Model(model_config=model_config, model_client_config=model_client_config)

    async def _get_scope_config(self, scope_id: str) -> MemoryScopeConfig | None:
        """
        Get the scope-specific configuration from memory cache first, then from kv_store if not found.

        Args:
            scope_id: Unique identifier for the scope

        Returns:
            MemoryScopeConfig: scope-specific configuration or None if not found
        """
        # First check if config is in memory cache
        if scope_id in self._scope_config:
            config = self._scope_config[scope_id]

            # Create a copy to avoid modifying the encrypted config in memory
            decrypted_config = copy.deepcopy(config)

            # Decrypt API keys if they exist
            if decrypted_config.model_client_cfg and decrypted_config.model_client_cfg.api_key:
                decrypted_config.model_client_cfg.api_key = self._storage_codec.decode(
                    decrypted_config.model_client_cfg.api_key
                )

            if decrypted_config.embedding_cfg and decrypted_config.embedding_cfg.api_key:
                decrypted_config.embedding_cfg.api_key = self._storage_codec.decode(
                    decrypted_config.embedding_cfg.api_key
                )

            return decrypted_config

        # If not in memory, get from kv_store
        return await self.get_scope_config(scope_id)

    async def _apply_scope_embedding(self, scope_id: str) -> None:
        """
        Apply the scope-specific embedding model to the memory_index.

        Retrieves the embedding model for the given scope from cache / scope config
        and updates the memory_index so that subsequent add / search operations
        use the correct embedding model.
        """
        if not self.memory_index:
            return

        scope_embed = await self._get_scope_embedding_model(scope_id)
        if scope_embed is not None:
            if hasattr(self.memory_index, 'set_embedding_model'):
                self.memory_index.set_embedding_model(scope_embed)
        else:
            if hasattr(self.memory_index, 'set_embedding_model'):
                self.memory_index.set_embedding_model(self._base_embed)

    async def _get_scope_embedding_model(self, scope_id: str) -> Embedding | None:
        """
        Get the embedding model for the scope from cache first, then from config if not found.

        Args:
            scope_id: scope/scope identifier

        Returns:
            APIEmbedModel: Embedding model for the scope, or None if no model is available
        """
        # Check if embedding model is already in cache
        if scope_id in self._scope_embedding:
            return self._scope_embedding[scope_id]

        try:
            config = await self._get_scope_config(scope_id)
            if config and config.embedding_cfg:
                # Use APIEmbedding to instantiate the embedding model
                embedding_model = APIEmbedding(config=config.embedding_cfg)
                # Cache the embedding model
                self._scope_embedding[scope_id] = embedding_model
                return embedding_model
            elif self._base_embed:
                # Fallback to base embedding model if no scope-specific config
                self._scope_embedding[scope_id] = self._base_embed
                return self._base_embed
        except Exception as e:
            memory_logger.error(
                "Failed to get or instantiate embedding model.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                scope_id=scope_id,
                exception=str(e)
            )

        memory_logger.error(
            "No embedding model available.",
            event_type=LogEventType.MEMORY_RETRIEVE,
            scope_id=scope_id
        )
        return None

    async def start_dreaming(
        self,
        scope_id: str,
        user_id: str,
        *,
        config: DreamingConfig | None = None,
        busy_checker: Callable[[], bool] | None = None,
    ) -> DreamingOrchestrator | None:
        """
        Start the sleep-time consolidation loop for a (scope_id, user_id).

        The loop runs **two independent sub-flows** per tick, controlled by
        separate flags on ``DreamingConfig`` / ``ForgettingConfig``:

        - ``config.enabled`` (DreamingConfig.enabled) controls whether the
          sweeper (compress → extract → promote) runs. Only the dreaming
          sub-flow is gated by this flag.
        - ``config.forgetting.enabled`` (ForgettingConfig.enabled) controls
          whether the Ebbinghaus forgetting step runs. Only the forgetting
          sub-flow is gated by this flag.

        Both sub-flows share the same orchestrator thread and the same
        ``interval_seconds`` tick — they are co-scheduled, not independent
        loops. If **both** flags are False, ``None`` is returned and no
        orchestrator is started. If either is True, the orchestrator runs
        and each tick dispatches whichever sub-flow(s) are enabled.

        Idempotent: a second call for the same (scope_id, user_id) returns
        the existing orchestrator.

        When only forgetting is enabled (dreaming disabled), the sweeper
        is skipped — no session reads, no LLM extraction, no new writes.
        This is the common case for tenants who want Ebbinghaus forgetting
        without offline consolidation.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_STORE, scope_id=scope_id):
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="dreaming",
                error_msg="invalid scope_id format",
            )

        config = config or DreamingConfig()
        # Forgetting config defaults to a fresh ForgettingConfig() (which
        # itself defaults to enabled=False). Treating None as
        # "ForgettingConfig(enabled=False)" keeps the downstream code path
        # uniform — there's always a forgetting_config object to read.
        forgetting_config = config.forgetting or ForgettingConfig()

        dream_enabled = config.enabled
        forget_enabled = forgetting_config.enabled
        if not dream_enabled and not forget_enabled:
            memory_logger.info(
                "Dreaming and forgetting both disabled by config, not starting.",
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id, scope_id=scope_id,
            )
            return None

        if not self.message_manager or not self.write_manager or not self.kv_store:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="dreaming",
                error_msg="stores not registered; call register_store before start_dreaming",
            )

        key = (scope_id, user_id)
        existing = self._dreaming_orchestrators.get(key)
        if existing is not None:
            return existing

        llm = await self._get_scope_llm(scope_id)
        if not llm:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="dreaming",
                error_msg="LLM is not initialized",
            )

        source = MessageStoreSessionSource(
            self.message_manager, user_id, scope_id,
            min_rounds=config.min_session_rounds,
            max_sessions=config.max_sessions_per_sweep,
        )
        store = MemoryUnitKnowledgeStore(
            self.write_manager, self.kv_store, llm, user_id, scope_id,
            # apply the scope's embedding onto the shared memory_index before each write,
            # consistent with add_messages' _apply_scope_embedding
            prepare_write=lambda: self._apply_scope_embedding(scope_id),
        )
        # Pull the scope's important_memory_definition (defaulting to the
        # global default when the scope isn't registered or the read
        # fails) so sweeper can inject it into the dreaming_extraction
        # prompt. Failures here must not break start_dreaming — the
        # default definition is reasonable.
        try:
            scope_config = await self._get_scope_config(scope_id)
            important_definition = (
                scope_config.important_memory_definition
                if scope_config and scope_config.important_memory_definition
                else MemoryScopeConfig().important_memory_definition
            )
        except Exception as exc:
            memory_logger.warning(
                "start_dreaming: failed to read scope config for "
                "important_memory_definition, using global default: %s",
                str(exc),
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id, scope_id=scope_id,
            )
            important_definition = MemoryScopeConfig().important_memory_definition
        sweeper = Sweeper(
            source=source, store=store, llm=llm, config=config,
            checkpoint_io=self.kv_store, user_id=user_id, scope_id=scope_id,
            important_memory_definition=important_definition,
        )

        # Build the sweep closure. The two sub-flows are independently
        # gated by their own flags:
        #   - sweeper.run_sweep() runs only when config.enabled (dreaming)
        #   - _forget_memories_step runs only when forgetting_config.enabled
        # Both share the same orchestrator tick and the same user-level
        # lock semantics; forgetting still runs AFTER sweeper when both are
        # enabled, so newly-promoted memories get their retrieve_history
        # seeded before being scored.
        async def sweep_fn() -> None:
            if dream_enabled:
                await sweeper.run_sweep()
            if not forget_enabled:
                return
            # _forget_memories_step already downgrades internal failures
            # to memory_logger.warning; the outer try/except is defence
            # in depth for anything unexpected.
            try:
                await self._forget_memories_step(
                    scope_id=scope_id,
                    user_id=user_id,
                    forgetting_config=forgetting_config,
                )
            except Exception as exc:
                memory_logger.warning(
                    "forget step failed, will retry next cycle: %s",
                    str(exc),
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=user_id, scope_id=scope_id,
                )

        orch = DreamingOrchestrator(
            sweep_fn, config.interval_seconds, busy_checker,
            name=f"dreaming/{scope_id}/{user_id}",
        )
        self._dreaming_orchestrators[key] = orch
        await orch.start()
        memory_logger.info(
            "Dreaming orchestrator started (dreaming=%s, forgetting=%s).",
            dream_enabled, forget_enabled,
            event_type=LogEventType.MEMORY_STORE,
            user_id=user_id, scope_id=scope_id,
        )
        return orch

    async def stop_dreaming(self, scope_id: str | None = None, user_id: str | None = None) -> None:
        """
        Stop dreaming. With no args, stop everything; otherwise stop the
        orchestrators matching the provided scope_id and/or user_id.
        """
        to_stop = [
            (key, orch)
            for key, orch in self._dreaming_orchestrators.items()
            if (scope_id is None or key[0] == scope_id) and (user_id is None or key[1] == user_id)
        ]
        for key, orch in to_stop:
            self._dreaming_orchestrators.pop(key, None)
            await orch.stop()

    # --- Ebbinghaus forgetting -----------------------------------------------
    # Step 6: the forgetting step that runs inside dreaming.
    # Marked-blacklisted memories are kept in storage but excluded from normal
    # search via the ``NE("blacklisted", True)`` FilterGroup (Step 7).
    #
    # Implementation notes:
    #   - ``DistributedLock(store, lock_name)`` takes no ``timeout`` parameter.
    #   - ``update_mem_by_id(user_id, scope_id, mem_id, fields: dict)`` takes
    #     a dict, not keyword args.
    #   - ``ForgetEvaluator.evaluate`` is ``async``, so we ``await`` it here.

    #: mem_types the forget sweep scans. Variable / middle_term are excluded
    #: (variable is tiny + ephemeral; middle_term is a separate hot-cache).
    _FORGET_MEM_TYPES = (
        MemoryType.USER_PROFILE.value,
        MemoryType.SEMANTIC_MEMORY.value,
        MemoryType.EPISODIC_MEMORY.value,
        MemoryType.SUMMARY.value,
    )

    async def _mark_blacklisted(
        self,
        *,
        scope_id: str,
        user_id: str,
        evicted: list[MemoryDoc],
    ) -> None:
        """
        Mark evicted memories as forgotten by flipping their vector scalar
        ``blacklisted`` field to ``True``.

        The vector scalar is the single source of truth for "is this memory
        forgotten": search and list paths filter via ``NE("blacklisted", True)``
        / ``EQ("blacklisted", True)``, so a successful flip is sufficient.

        Failures are logged and swallowed — a single bad doc doesn't abort
        the rest of the batch. Failed ids are simply skipped; the next sweep
        will retry the flip (the operation is idempotent).
        """
        if not evicted:
            return

        for doc in evicted:
            try:
                await self.memory_index.update_mem_by_id(
                    user_id, scope_id, doc.id, {"blacklisted": True}
                )
            except Exception as exc:
                memory_logger.warning(
                    "mark blacklisted (vector) failed for %s: %s",
                    doc.id, exc,
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=user_id, scope_id=scope_id, mem_id=doc.id,
                )

    async def _forget_memories_step(
        self,
        *,
        scope_id: str,
        user_id: str,
        forgetting_config: "ForgettingConfig",
    ) -> None:
        """
        Sleep-time forgetting sweep. Orchestrates only: lock → sweep →
        delegate to evaluator → mark blacklist. The scoring / eviction
        decision is fully owned by ``ForgetEvaluator.evaluate``.

        Failures (lock contention, list_memories exception, evaluator
        exception) are downgraded to ``memory_logger.warning`` and do NOT
        abort the surrounding dreaming sweep — the next interval retry
        is the recovery path.
        """
        # The evaluator may be None → fall back to the built-in Ebbinghaus
        # formula with this instance's KV store.
        evaluator = forgetting_config.evaluator or EbbinghausEvaluator(self.kv_store)
        ctx = ForgetContext(
            scope_id=scope_id,
            user_id=user_id,
            now=datetime.now(timezone.utc).astimezone(),
            forgetting_config=forgetting_config,
        )

        # Independent user-level lock — sweeper.run_sweep takes the same
        # lock but releases it after each write; forget must hold its own
        # to avoid racing add_messages / other writers during the sweep.
        try:
            async with DistributedLock(self.kv_store, f"user/{user_id}"):
                # Paginated sweep of the candidate set. No filters here
                # — we scan the whole eligible mem_type set including
                # already-blacklisted (they'll just be re-marked, idempotent).
                memories: list[MemoryDoc] = []
                offset = 0
                page_size = 100
                while True:
                    batch = await self.memory_index.list_memories(
                        user_id, scope_id,
                        offset=offset, limit=page_size,
                        mem_types=list(self._FORGET_MEM_TYPES),
                    )
                    if not batch:
                        break
                    memories.extend(batch)
                    offset += page_size
                    if len(batch) < page_size:
                        break

                # Delegate scoring + eviction decision to the evaluator
                # (Ebbinghaus by default; pluggable via ForgettingConfig).
                evicted = await evaluator.evaluate(ctx, memories)

                # Apply blacklist (vector scalar flip — single source of
                # truth; the next sweep will retry any id whose flip failed).
                await self._mark_blacklisted(
                    scope_id=scope_id, user_id=user_id, evicted=evicted,
                )
        except Exception as exc:
            memory_logger.warning(
                "forget_memories_step failed, will retry next sweep: %s",
                str(exc),
                event_type=LogEventType.MEMORY_PROCESS,
                user_id=user_id, scope_id=scope_id,
            )

    async def _get_scope_llm(self, scope_id: str) -> Model:
        """
        Get LLM for the scope.

        Args:
            scope_id: scope/scope identifier

        Returns:
            Model: LLM instance
        """
        try:
            config = await self._get_scope_config(scope_id)

            if config and config.model_cfg and config.model_client_cfg:
                return LongTermMemory._get_llm_from_config(config.model_cfg, config.model_client_cfg)

            # If the LLM fails to be obtained, try to use the system default configuration.
            elif not self._sys_mem_config:
                pass
            elif not self._sys_mem_config.default_model_client_cfg:
                memory_logger.debug(
                    "Default model client config is missing, cannot instantiate LLM.",
                    event_type=LogEventType.MEMORY_RETRIEVE,
                    scope_id=scope_id
                )
            elif not self._sys_mem_config.default_model_cfg:
                memory_logger.debug(
                    "Default model config is missing, cannot instantiate LLM.",
                    event_type=LogEventType.MEMORY_RETRIEVE,
                    scope_id=scope_id
                )
            else:
                return LongTermMemory._get_llm_from_config(self._sys_mem_config.default_model_cfg,
                                                         self._sys_mem_config.default_model_client_cfg)
            return self._base_llm

        except Exception as e:
            memory_logger.error(
                "Failed to get scope LLM.",
                event_type=LogEventType.MEMORY_RETRIEVE,
                scope_id=scope_id,
                exception=str(e)
            )
            # If the LLM fails to be obtained, try to use the system default configuration.
            return self._base_llm

    def _check_messages(self, messages: list[BaseMessage]) -> Tuple[bool, list[BaseMessage]]:
        out_messages = []
        has_human_msg = False
        human_message: UserMessage = UserMessage()
        for msg in messages:
            if msg.role == human_message.role:
                out_messages.append(msg)
                has_human_msg = True
                continue
            msg.content = msg.content[:self._sys_mem_config.input_msg_max_len]
            out_messages.append(msg)

        return has_human_msg, out_messages

    async def _get_history_messages(self,
                                    user_id: str,
                                    scope_id: str,
                                    session_id: str,
                                    history_window_size: int
                                    ) -> list[BaseMessage]:
        threshold = history_window_size
        if not self.message_manager:
            return []
        history_messages_tuple = await self.message_manager.get(
            user_id=user_id,
            scope_id=scope_id,
            session_id=session_id,
            message_len=threshold
        )
        history_messages = []
        human_message: UserMessage = UserMessage()
        for msg, _, _ in history_messages_tuple:
            if msg.role == human_message.role:
                history_messages.append(msg)
                continue
            msg.content = msg.content[:self._sys_mem_config.input_msg_max_len]
            history_messages.append(msg)
        return history_messages

    @staticmethod
    def _validate_id(event_type: LogEventType, scope_id: str = "") -> bool:
        """
        Validate the scope_id format.

        Args:
            scope_id: Scope identifier

        Returns:
            True if the scope_id is valid, False otherwise.
        """
        if not scope_id:
            memory_logger.error(
                "Scope_id is invalid.",
                event_type=event_type,
                scope_id=scope_id
            )
            return False
        if "/" in scope_id:
            memory_logger.error(
                "Scope_id cannot contain separator '/'.",
                event_type=event_type,
                scope_id=scope_id
            )
            return False
        if len(scope_id) > 128:
            memory_logger.error(
                "Scope_id length exceeds limit (128).",
                event_type=event_type,
                scope_id=scope_id
            )
            return False
        return True

    async def _run_migration(self, migrate_func, store, store_type: str):
        """
        Execute a migration with unified logging and error handling.

        Args:
            migrate_func: The migration function to execute
            store: The store instance to pass to the migration function
            store_type: Type of store for error messages and logging (e.g., "kv store")
        """
        try:
            memory_logger.info(f"Starting {store_type} migration", event_type=LogEventType.MEMORY_INIT)
            await migrate_func(store)
            memory_logger.info(f"{store_type} migration completed successfully", event_type=LogEventType.MEMORY_INIT)
        except Exception as e:
            memory_logger.error(f"{store_type} migration failed", event_type=LogEventType.MEMORY_INIT, exception=str(e))
            raise build_error(
                StatusCode.MEMORY_REGISTER_STORE_EXECUTION_ERROR,
                store_type=store_type,
                error_msg=f"{store_type} migration failed: {str(e)}",
                cause=e
            ) from e

    async def _create_semantic_store_with_embedding(self, scope_id: str) -> SemanticStore:
        """Create a new semantic store instance and initialize it with the appropriate embedding model.

        Args:
            scope_id: Scope identifier

        Returns:
            SemanticStore: New semantic store instance with embedding model initialized

        """
        semantic_store = SemanticStore(vector_store=self.vector_store)
        embedding_model = await self._get_scope_embedding_model(scope_id)
        if embedding_model:
            semantic_store.initialize_embedding_model(embedding_model)
        elif self._base_embed:
            semantic_store.initialize_embedding_model(self._base_embed)
        else:
            pass
        return semantic_store
