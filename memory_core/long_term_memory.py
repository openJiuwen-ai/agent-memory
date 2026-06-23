# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import copy
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Tuple
from pydantic import BaseModel, Field

from foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
from foundation.llm import Model, UserMessage, AssistantMessage, BaseMessage
from memory_core.common.distributed_lock import DistributedLock
from memory_core.config.config import MemoryEngineConfig, MemoryScopeConfig, AgentMemoryConfig, DreamingConfig
from memory_core.process.extract.generation import Generator
from memory_core.process.dreaming import (
    DreamingOrchestrator,
    MemoryUnitKnowledgeStore,
    MessageStoreSessionSource,
    Sweeper,
)
from memory_core.manage.mem_model.data_id_manager import DataIdManager
from memory_core.manage.mem_model.message_manager import MessageManager, MessageAddRequest
from foundation.store.base_message_store import BaseMessageStore
from memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager
from memory_core.manage.index.variable_manager import VariableManager
from memory_core.manage.index.write_manager import WriteManager
from memory_core.manage.index.summary_manager import SummaryManager
from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager, MemoryType
from memory_core.manage.mem_model.memory_unit import FragmentMemoryUnit, MemoryType,\
    SummaryUnit, VariableUnit
from memory_core.manage.search.search_manager import SearchManager, SearchParams
from foundation.store.base_db_store import BaseDbStore
from foundation.store.base_kv_store import BaseKVStore
from memory_core.manage.mem_model.db_model import create_tables
from memory_core.manage.mem_model.sql_db_store import SqlDbStore
from memory_core.manage.mem_model.sql_message_store import SqlMessageStore
from foundation.llm import UserMessage, BaseMessage, Model
from common.utils.singleton import Singleton
from retrieval.embedding.base import Embedding
from retrieval.embedding.api_embedding import APIEmbedding
from foundation.store.base_vector_store import BaseVectorStore
from foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from foundation.store.index.simple_memory_index import SimpleMemoryIndex
from memory_core.manage.mem_model.scope_user_mapping_manager import ScopeUserMappingManager
from common.exception.codes import StatusCode
from common.exception.errors import build_error
from common.logging import memory_logger
from common.logging.events import LogEventType
from memory_core.migration.run_migrations import run_kv_migrations,\
    run_vector_migrations, run_sql_migrations, run_message_migrations
from memory_core.codec.aes_storage_codec import AesStorageCodec
from memory_core.manage.mem_model.semantic_store import SemanticStore


class MemInfo(BaseModel):
    mem_id: str = Field(default="", description="memory id")
    content: str = Field(default="", description="memory content")
    type: MemoryType = Field(default=MemoryType.USER_PROFILE, description="memory type")
    timestamp: datetime | None = Field(default=None, description="memory timestamp")


class MemResult(BaseModel):
    mem_info: MemInfo = Field(default=None, description="memory information")
    score: float = Field(default=0.0, description="memory score of relevance")


class AddMemResult(BaseModel):
    variables: list[VariableUnit] = Field(default=list, description="variables result")
    user_profile: list[FragmentMemoryUnit] = Field(default=list, description="user_profile memory result")
    semantic_memory: list[FragmentMemoryUnit] = Field(default=list, description="semantic memory result")
    episodic_memory: list[FragmentMemoryUnit] = Field(default=list, description="episodic memory result")
    summary: list[SummaryUnit] = Field(default=list, description="summary result")


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
        self._storage_codec: AesStorageCodec | None = None
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
        self._stop_event = threading.Event()
        self._thread_running = False
        self._background_task = None
        self._background_task_running = False
        self._agent_config = None  # Save configuration
        # Thread pool: for batch concurrent processing
        self._batch_executor = ThreadPoolExecutor(
            max_workers=20,  # Adjust based on LLM API concurrency limits
            thread_name_prefix="batch_memory_processor",
        )
        self._executor_active = True

        self._enable_hierarchical_memory = True


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
                             message_store: BaseMessageStore | None = None):
        """
        Register store instance.

        Args:
            kv_store: Key-value store for fast structured data access
            vector_store: Vector storage for vector-based similarity search
            db_store: Database store for persistent data storage
            embedding_model: Embedding model for semantic search
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

        self.kv_store = kv_store
        self.vector_store = vector_store
        self.db_store = db_store
        self._base_embed = embedding_model
        self.message_store = message_store

        # Auto register SimpleMemoryIndex if vector_store is provided
        if self.vector_store and self.kv_store:
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
                        fields=doc.fields.copy()
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
        self._sys_mem_config = config

        codec = AesStorageCodec(config.crypto_key)
        if self.memory_index:
            self.memory_index.set_storage_codec(codec)
        self._storage_codec = codec

        data_id_generator = DataIdManager()

        sql_db_store = SqlDbStore(self.db_store) if self.db_store else None
        if sql_db_store:
            self.scope_user_mapping_manager = ScopeUserMappingManager(sql_db_store)

        if self.message_store:
            if isinstance(self.message_store, SqlMessageStore) and self.message_store.crypto_key is None:
                self.message_store.crypto_key = config.crypto_key
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
            config.crypto_key
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
        self.write_manager = WriteManager(managers, self.memory_index)
        self.middle_write_manager = WriteManager(middle_managers, self.memory_index)

        self.search_manager = SearchManager(
            managers,
            config.crypto_key,
            self.memory_index
        )

        self.middle_search_manager = SearchManager(
            middle_managers,
            config.crypto_key,
            self.memory_index
        )

        self.generator = Generator(data_id_generator=data_id_generator, search_manager=self.search_manager)
        # set init llm
        if config.default_model_cfg and config.default_model_client_cfg:
            llm = LongTermMemory._get_llm_from_config(model_config=config.default_model_cfg,
                                                    model_client_config=config.default_model_client_cfg)
            self._base_llm = llm

    def start(self):
        """[Daemon thread mode] Start background tasks, continue running after main program ends"""
        if self._enable_hierarchical_memory:
            if self._thread_running:
                memory_logger.warning("Middle memory processor is already running")
                return

            agent_config = AgentMemoryConfig(
                mem_variables=[],
                enable_long_term_mem=True,
                enable_user_profile=True,
                enable_semantic_memory=True,
                enable_episodic_memory=True,
                enable_summary_memory=True,
            )

            self._stop_event.clear()
            self._thread_running = True

            # ✅ Key: Create independent daemon thread, detach from main process lifecycle
            self.thread = threading.Thread(
                target=self._start_async_loop_in_thread,
                args=(agent_config,),
                daemon=True,
                name="MiddleMemoryThread",
            )
            self.thread.start()
            memory_logger.info("Middle memory processor started successfully (daemon thread mode)")

        pass

    def _start_async_loop_in_thread(self, agent_config):
        """Run async event loop independently within thread"""
        if self._enable_hierarchical_memory:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._middle_memory_loop(agent_config))
            finally:
                loop.close()
        else:
            pass

    def stop(self):
        """Stop background tasks (safe shutdown)"""
        if self._enable_hierarchical_memory:
            self._stop_event.set()

            # Close thread pool
            if self._batch_executor and self._executor_active:
                memory_logger.info("Shutting down batch executor...")
                self._batch_executor.shutdown(wait=True)
                self._executor_active = False

            self.thread.join()
            memory_logger.info("Middle memory processor stopped")
        else:
            pass

    def _run_batch_in_thread(
        self, batch_data: dict, agent_config: AgentMemoryConfig, user_id: str, scope_id: str, session_id: str
    ):
        """Run batch processing in independent thread
        Each thread creates independent event loop
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                self._process_dialogue_batch_to_long_term_safe(
                    dialogue_batch=batch_data["dialogues"],
                    agent_config=agent_config,
                    user_id=user_id,
                    scope_id=scope_id,
                    session_id=session_id,
                    timestamp_str=batch_data["timestamp"],
                    mem_ids=batch_data["mem_ids"],
                )
            )
            return result
        except Exception as e:
            import traceback
            memory_logger.error(
                f"Batch processing failed in thread: {str(e)}", exception=str(e), traceback=traceback.format_exc()
            )
            return {"success": False, "mem_ids": batch_data["mem_ids"], "error": str(e)}
        finally:
            loop.close()

    async def _process_dialogue_batch_to_long_term_safe(
        self, dialogue_batch, agent_config, user_id, scope_id, session_id, timestamp_str, mem_ids
    ):
        """Safe batch processing version, for thread pool calls
        Each batch independently gets LLM and semantic_store, avoid concurrency conflicts
        """

        if not dialogue_batch:
            return {"success": False, "mem_ids": [], "error": "Empty batch"}

        try:
            print(f"[Thread] Processing batch with {len(dialogue_batch)} dialogues")
            # Each thread independently gets resources (avoid concurrency conflicts)
            llm = await self._get_scope_llm(scope_id)
            semantic_store = await self._create_semantic_store_with_embedding(scope_id)
            scope_config = await self._get_scope_config(scope_id)

            if not llm:
                return {"success": False, "mem_ids": mem_ids, "error": "LLM not ready"}

            print(f"[Thread] LLM ready, parsing dialogues...")
            # Parse conversation
            converted = []
            for dialogue in dialogue_batch:
                try:
                    if "User：" in dialogue or "Assistant：" in dialogue:
                        parsed_msgs = await self.parse_str_to_messages(dialogue)
                        converted.extend(parsed_msgs)
                    else:
                        converted.append(UserMessage(role="user", content=dialogue))
                except Exception as e:
                    memory_logger.warning(f"Failed to parse dialogue: {str(e)}")
                    continue

            if not converted:
                return {"success": False, "mem_ids": mem_ids, "error": "No valid messages"}
            print(f"[Thread] Parsed {len(converted)} messages, extracting memories...")
            check_res, valid_msgs = self._check_messages(converted)

            if not check_res:
                return {"success": False, "mem_ids": mem_ids, "error": "No valid user messages"}

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
                print(f"[Thread] Storing {len(memories)} long-term memories")
                await self.write_manager.add_memories(
                    user_id=user_id, scope_id=scope_id, memories=memories, llm=llm
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

    async def _batch_delete_middle_memories(self, mem_ids: list):
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

    async def _middle_memory_loop(self, agent_config: AgentMemoryConfig):
        """Async background loop: automatically execute middle_mem_to_long"""
        memory_logger.info("Middle memory background loop started")

        # Wait for initialization to complete
        await asyncio.sleep(3)

        while not self._stop_event.is_set():
            try:
                memory_logger.info("=== Executing middle_mem_to_long ===")
                await self.middle_mem_to_long(
                    agent_config=agent_config, user_id="default", scope_id="default"
                )
                # Execute every 10 seconds for easy testing
                await asyncio.sleep(50)

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
        user_ids = [scope_user["user_id"] for scope_user in scope_user_data]
        if self.write_manager:
            for user_id in user_ids:
                lock = DistributedLock(self.kv_store, f"user/{user_id}")
                async with lock:
                    await self.write_manager.delete_mem_by_user_id(
                        scope_id=scope_id,
                        user_id=user_id
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
        if self._enable_hierarchical_memory:
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
                # if timestamp is None, take the current time
                if not timestamp:
                    timestamp = datetime.now(timezone.utc).astimezone()
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
                            scope_id="middle_term_memory",
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
                # if timestamp is None, take the current time
                if not timestamp:
                    timestamp = datetime.now(timezone.utc).astimezone()
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
                        llm=llm
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
        timestamp: datetime | None = None,
        gen_mem: bool = True,
    ):
        """[Core] Middle-term memory -> Long-term memory (thread pool concurrent version)
        """
        try:
            # Step 1: Get middle-term memories
            middle_messages_all = await self.message_manager.get(scope_id="middle_term_memory", message_len=100)
            print("middle_messages_all", middle_messages_all)

            if not middle_messages_all:
                memory_logger.info("No middle memories to process")
                return

            print(f"Processing {len(middle_messages_all)} middle memories")

            # Step 2: Continuity check + batch partitioning (keep serial)
            batches = []
            dialogue_batch = []
            pre_dialogue = ""
            batch_mem_ids = []
            pre_timestamp = ""

            for each in middle_messages_all:
                cur_dialogue = each[0].content
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
                continuity_results = await self.check_dialogue_continuity(
                    scope_id=scope_id, previous_dialogue=pre_dialogue, current_dialogue=cur_dialogue
                )

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

            print(f"Split into {len(batches)} batches for concurrent processing")
            memory_logger.info(f"Split {len(middle_messages_all)} memories into {len(batches)} batches")

            # Step 3: Thread pool concurrent processing of all batches
            if not batches:
                return

            # Use asyncio + ThreadPoolExecutor for concurrency
            loop = asyncio.get_running_loop()
            futures = []

            for i, batch_data in enumerate(batches):
                # Submit each batch to thread pool
                future = loop.run_in_executor(
                    self._batch_executor,
                    self._run_batch_in_thread,
                    batch_data,
                    agent_config,
                    user_id,
                    scope_id,
                    session_id,
                )
                futures.append(future)
                print(f"Batch {i + 1} submitted to thread pool (size: {len(batch_data['dialogues'])})")

            # Step 4: Wait for all batches to complete
            memory_logger.info(f"Waiting for {len(futures)} batch processing tasks to complete...")
            results = await asyncio.gather(*futures, return_exceptions=True)

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
                    print(
                        f"Batch {i + 1} succeeded: processed {result.get('batch_size')} dialogues, "
                        f"extracted {result.get('memories_count', 0)} memories"
                    )
                else:
                    failed_batches += 1
                    error_msg = result.get("error", "Unknown error") if result else "No result"
                    memory_logger.warning(f"Batch {i + 1} failed: {error_msg}")

            # Step 6: Batch delete processed middle-term memories
            if all_mem_ids_to_delete:
                print(f"Deleting {len(all_mem_ids_to_delete)} processed middle memories in batch")
                memory_logger.info(f"Batch deleting {len(all_mem_ids_to_delete)} middle memories")

                try:
                    await self._batch_delete_middle_memories(all_mem_ids_to_delete)
                except Exception as e:
                    memory_logger.error(f"Failed to batch delete middle memories: {str(e)}", exception=str(e))

            # Step 7: Output statistics
            print(f"Processing completed: {successful_batches}/{len(batches)} batches succeeded")
            memory_logger.info(
                f"Middle memory conversion completed: {successful_batches} succeeded, "
                f"{failed_batches} failed, {len(all_mem_ids_to_delete)} memories deleted"
            )

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"middle_mem_to_long failed: {str(e)}\n{tb}")
            memory_logger.error("middle_mem_to_long failed", exception=str(e), traceback=traceback.format_exc())

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
        recent_messages = [msg for msg, _ in recent_messages_tuple]
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
                mem_id=mem_id
            )

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
            await self.write_manager.update_mem_by_id(user_id=user_id, scope_id=scope_id,
                                                      mem_id=mem_id, memory=memory)

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

    async def search_user_mem(self,
                              query: str,
                              num: int,
                              user_id: str = DEFAULT_VALUE,
                              scope_id: str = DEFAULT_VALUE,
                              threshold: float = 0.3
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
            search_type=self.fragment_type
        )
        try:
            search_data = []
            search_data = await self.search_manager.search(params)

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
                                   memory_type: MemoryType = MemoryType.UNKNOWN) -> list[MemInfo]:
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
        if not self.search_manager:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="all",
                error_msg=f"search manager is not initialized",
            )

        if memory_type == MemoryType.UNKNOWN:
            search_memory_type = None
        else:
            search_memory_type = memory_type.value
        search_data = await self.search_manager.list_user_mem(user_id=user_id, scope_id=scope_id,
                                                              nums=page_size, pages=page_idx,
                                                              mem_type=search_memory_type)

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
        Start background dreaming (offline cross-session consolidation) for a
        (scope_id, user_id).

        Reuses this instance's ``_get_scope_llm`` / ``message_manager`` /
        ``write_manager`` / ``kv_store``; promotes extracted knowledge through the
        normal memory write path (vector store).

        Idempotent: a second call for the same (scope_id, user_id) returns the
        existing orchestrator. Returns ``None`` when ``config.enabled`` is False.
        """
        if not self._validate_id(event_type=LogEventType.MEMORY_STORE, scope_id=scope_id):
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="dreaming",
                error_msg="invalid scope_id format",
            )

        config = config or DreamingConfig()
        if not config.enabled:
            memory_logger.info(
                "Dreaming disabled by config, not starting.",
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
        sweeper = Sweeper(
            source=source, store=store, llm=llm, config=config,
            checkpoint_io=self.kv_store, user_id=user_id, scope_id=scope_id,
        )
        orch = DreamingOrchestrator(
            sweeper.run_sweep, config.interval_seconds, busy_checker,
            name=f"dreaming/{scope_id}/{user_id}",
        )
        self._dreaming_orchestrators[key] = orch
        await orch.start()
        memory_logger.info(
            "Dreaming started.",
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
        for msg, _ in history_messages_tuple:
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