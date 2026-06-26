# openJiuwen Memory

[中文版](README.zh.md) | [English Version](README.md)

## Introduction

**openJiuwen Memory** is a long-term memory module for AI agents, providing memory extraction, storage, retrieval, and migration capabilities for agents running on the **openJiuwen** framework. This module not only supports automatic extraction and structured management of multiple memory types — including user profile, semantic memory, episodic memory, variable memory, and summary memory — but also features built-in vector-based semantic search, flexible integration with multiple storage backends (KV store, vector database, relational database), and data migration capabilities. Additionally, it provides a pluggable MemoryProvider mechanism that supports seamless integration with third-party memory services such as Mem0 and AgentArts. **openJiuwen Memory** balances flexibility and security, helping developers efficiently build intelligent agent applications with persistent memory capabilities.

## Why Choose openJiuwen Memory?

- **Multi-type Automatic Memory Extraction**: Through intelligent analysis of conversation content, automatically extracts multiple types of memory including user profile, semantic memory, episodic memory, variables, and summaries — no manual rule configuration required, significantly lowering the development barrier for memory management.

- **Flexible and Extensible Storage Architecture**: Built-in abstract interfaces for multiple storage backends including KV store, vector database, and relational database, supporting Milvus, ChromaDB, PostgreSQL, MySQL, GaussDB, SQLite, Redis, and more — developers can choose and replace as needed.

- **Efficient and Precise Semantic Retrieval**: Vector embedding-based semantic search capability with unified cross-memory-type retrieval, combined with similarity threshold filtering and ranking mechanisms to ensure high relevance and precision of search results.

- **Structured Knowledge Graph Memory**: Graph Memory writes entities, relations, and source episodes from conversations, documents, or JSON strings into graph storage, supporting entity merging, relation deduplication, hybrid retrieval, and graph expansion recall.

- **Secure and Reliable Data Management**: Built-in AES-GCM encryption codec supporting encrypted storage and transmission of memory data; distributed lock mechanism ensuring data consistency under concurrent scenarios; fine-grained configuration management by scope.

- **Flexible Data Migration**: A complete versioned migration framework supporting schema migration for KV store, vector database, SQL database, and message store, as well as cross-index data migration for smooth upgrades.

## Quick Start

### Installation

- Operating System: Compatible with Windows, Linux, and macOS.
- Python Version: Python version should be 3.11 or higher, but lower than 3.14. Please check your Python version before use, Python 3.11.4 is recommended.

**Install from PyPI**

```bash
pip install -U JiuwenMemory
```

**Install Optional Storage Backends**

```bash
# SQLite support
pip install JiuwenMemory[sqlite]

# PostgreSQL support
pip install JiuwenMemory[postgres]

# MySQL support
pip install JiuwenMemory[mysql]

# GaussDB support
pip install JiuwenMemory[gaussdb]

# Redis support
pip install JiuwenMemory[redis]

# ChromaDB vector store
pip install JiuwenMemory[chromadb]

# Memory server (includes uvicorn + fastapi; enables `memory-server` CLI command)
pip install JiuwenMemory[server]

# Install all storage backends (includes server)
pip install JiuwenMemory[all]
```

### Example

Let's create a simple long-term memory instance, register storage backends, add conversation messages, and retrieve memories:

```python
import asyncio
import tempfile
from sqlalchemy.ext.asyncio import create_async_engine
from memory_core import LongTermMemory
from memory_core.config.config import MemoryEngineConfig, MemoryScopeConfig, AgentMemoryConfig, DreamingConfig
from foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from foundation.llm import UserMessage, AssistantMessage
from foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from foundation.store.db.default_db_store import DefaultDbStore
from foundation.store.vector.chroma_vector_store import ChromaVectorStore
from retrieval.embedding.api_embedding import APIEmbedding
from retrieval.common.config import EmbeddingConfig

# ============== Configuration: set directly in code, no .env required ==============
# LLM configuration
MODEL_PROVIDER = "xxxx"
API_BASE = "xxxx"
API_KEY = "xxxx"
MODEL_NAME = "xxxx"

# Embedding configuration
EMBED_MODEL_NAME = "xxxx"
EMBED_API_BASE = "xxxx"
EMBED_API_KEY = "xxxx"
# ====================================================================================


async def main():
    # Get the LongTermMemory singleton
    memory = LongTermMemory()

    # Create LLM configuration
    model_client_config = ModelClientConfig(
        client_provider=MODEL_PROVIDER,
        api_key=API_KEY,
        api_base=API_BASE,
        verify_ssl=False,
    )
    model_config = ModelRequestConfig(
        model=MODEL_NAME
    )

    # Create storage backends (example uses in-memory KV, SQLite, and ChromaDB)
    kv_store = InMemoryKVStore()
    engine = create_async_engine("sqlite+aiosqlite:///./memory.db")
    db_store = DefaultDbStore(engine)
    vector_store = ChromaVectorStore(persist_directory=tempfile.mkdtemp())

    # Create embedding model
    embedding_config = EmbeddingConfig(
        model_name=EMBED_MODEL_NAME,
        base_url=EMBED_API_BASE,
        api_key=EMBED_API_KEY,
    )
    embedding_model = APIEmbedding(config=embedding_config)

    # Register storage backends
    await memory.register_store(
        kv_store=kv_store,
        db_store=db_store,
        vector_store=vector_store,
        embedding_model=embedding_model,
    )

    # Configure memory engine
    engine_config = MemoryEngineConfig(
        default_model_cfg=model_config,
        default_model_client_cfg=model_client_config,
    )
    memory.set_config(engine_config)

    # Configure scope
    scope_config = MemoryScopeConfig(
        model_cfg=model_config,
        model_client_cfg=model_client_config,
        embedding_cfg=embedding_config,
    )
    await memory.set_scope_config(scope_id="my_app", memory_scope_config=scope_config)

    # Configure agent memory
    agent_config = AgentMemoryConfig(
        enable_long_term_mem=True,
        enable_user_profile=True,
        enable_semantic_memory=True,
        enable_episodic_memory=True,
        enable_summary_memory=True,
    )

    # Feed the conversation turn by turn — each turn is stored and extracted online in real time.
    conversation = [
        ("I'm a data analyst; I use pandas on our sales data, but my pipeline has gotten slow.",
         "You could try Polars — it's usually 5-10x faster than pandas and great for large datasets."),
        ("I also enjoy basketball and reading sci-fi novels.",
         "Nice balance! For hard sci-fi, Liu Cixin's 'The Three-Body Problem' is worth a read."),
        ("This afternoon I played basketball with friends at the park, it was really fun.",
         "Sounds like a great afternoon!"),
    ]
    for user_text, assistant_text in conversation:
        await memory.add_messages(
            messages=[UserMessage(content=user_text), AssistantMessage(content=assistant_text)],
            agent_config=agent_config,
            user_id="user_001",
            scope_id="my_app",
            session_id="session_001",
        )

    query = "how to speed up data processing"

    # Print all of this user's memories, then run one semantic search.
    async def show():
        print("  memories:")
        page = await memory.get_user_mem_by_page(user_id="user_001", scope_id="my_app", page_size=50)
        for m in page:
            print(f"    [{m.type.value}] {m.content}")
        print("  search:")
        for res in await memory.search_user_mem(query=query, num=5, user_id="user_001", scope_id="my_app"):
            print(f"    {res.mem_info.content} (relevance: {res.score:.2f})")

asyncio.run(main())
```

Expected Output (memory content is LLM-generated, so wording will vary):
```
===== before dreaming =====
  memories:
    [user_profile] User is a data analyst
    [user_profile] User uses pandas on sales data
    [user_profile] User enjoys basketball
    [user_profile] User enjoys reading sci-fi novels
    [episodic_memory] User played basketball with friends at the park on the afternoon of 2026-06-18   # date reflects the run date and will vary
  search:
    User uses pandas on sales data (relevance: 0.80)

===== after dreaming =====
  memories:
    [user_profile] User is a data analyst
    [user_profile] User uses pandas on sales data
    [user_profile] User enjoys basketball
    [user_profile] User enjoys reading sci-fi novels
    [episodic_memory] User played basketball with friends at the park on the afternoon of 2026-06-18
    [semantic_memory] Polars is usually 5-10x faster than pandas and suits large-scale data processing   # <- added by dreaming
  search:
    User uses pandas on sales data (relevance: 0.80)
    Polars is usually 5-10x faster than pandas and suits large-scale data processing (relevance: 0.86)
```

> Dreaming writes the same memory types through the same path as online extraction — so its
> output is ordinary `user_profile` / `semantic_memory` / `episodic_memory`, retrieved by the
> same `search_user_mem`. Its added value here is the **Polars tip**: a reusable fact the
> assistant supplied that the narrow per-turn window skipped, which the full-session sweep
> consolidates into memory.

## Architecture Design

**openJiuwen Memory** serves as the core module of the openJiuwen memory architecture. In this open-source version, the core capabilities include:

* **Memory Processing Layer**: Through intelligent analysis of conversation messages, automatically extracts five types of memory — user profile (UserProfile), semantic memory (SemanticMemory), episodic memory (EpisodicMemory), variable (Variable), and summary (Summary) — and supports custom extraction rules and instruct-based memory operations (add, update, delete).

* **Memory Management Layer**: Provides differentiated management strategies for different memory types, including FragmentMemoryManager, VariableManager, and SummaryManager, coordinated through WriteManager and SearchManager for unified read/write operations.

* **Storage Foundation Layer**: Provides abstract interfaces for four types of storage — KV store, vector store, relational database, and message store — supporting flexible integration with multiple storage backends, and ensuring smooth data schema upgrades through the versioned migration framework.

* **Graph Memory Layer**: Graph Memory uses LLMs to extract entities (Entity), relations (Relation), and source episodes (Episode) from conversations, documents, or JSON strings, then performs entity merging, relation deduplication, and graph-structured retrieval. It is currently used as an independent module and is not wired into the `LongTermMemory.add_messages` pipeline.

* **External Integration Layer**: Through the MemoryProvider abstract interface, supports seamless integration with third-party memory services such as Mem0, AgentArts, openJiuwen, and openViking, providing unified tool calling and session synchronization mechanisms.

## Features

### **Multi-type Memory Extraction**

**openJiuwen Memory** supports automatic extraction and management of five types of memory, with rich functionality and flexible development to meet intelligent needs in different scenarios.

- **User Profile**: Extracts affirmative or negative statements about the user, covering basic identity, interest preferences, interpersonal relationships, asset status, etc., building a personalized user profile.
- **Semantic Memory**: Extracts factual content or concepts from conversations that have no explicit temporal relationship, storing general knowledge-based information.
- **Episodic Memory**: Extracts factual content or concepts from conversations that have an explicit temporal relationship, recording timestamped event-based information.
- **Variable**: Extracts structured key-value pair information from conversations, supporting custom variable definitions and forbidden variable configurations.
- **Summary**: Automatically summarizes conversation content, supporting per-turn generation and incremental updates.

### **Dreaming (Offline Memory Consolidation)**

Online extraction (`add_messages`) only ever sees a single turn. **Dreaming** is an optional background service that periodically re-reads a user's stored sessions, distills durable knowledge from them, and writes it back through the same path as online extraction — dreamed memories are ordinary user profile / semantic / episodic units, with no new memory type or storage field.

- **Fire-and-forget lifecycle**: `start_dreaming(scope_id, user_id, config=DreamingConfig(enabled=True))` launches a background scheduler (idempotent per `(scope_id, user_id)`); `stop_dreaming()` stops it. Disabled by default.
- **Tunable via `DreamingConfig`**: sweep interval (`interval_seconds`), session pre-filters (`min_session_rounds`, `max_sessions_per_sweep`), and extraction caps (`max_compress_tokens`, `max_items_per_session`).
- **Checkpointed and concurrency-safe**: scanned sessions are checkpointed in the KV store and survive restarts; writes take the same user-level lock as `add_messages` and reuse semantic conflict detection, so online and offline writes never collide or duplicate.

### **Semantic Retrieval and Conflict Detection**

- **Vector Semantic Search**: Vector retrieval capability based on embedding models, supporting unified cross-memory-type search with similarity threshold filtering and ranking mechanisms.
- **Conflict Detection and Update**: Before writing new memories, automatically detects semantic conflicts with existing memories, determining whether to update or add through semantic validation to ensure memory consistency.
- **Instruct-based Memory Operations**: Supports updating and deleting existing memories through LLM output instructions, combined with semantic validation to ensure operational accuracy.

### **Graph Memory (Knowledge Graph Memory)**

Graph Memory is an independent knowledge graph memory module. It turns input content into a graph of entities, relations, and source episodes, suitable for relationship retrieval, entity tracking, and graph expansion recall.

- **Multi-source writes**: Supports conversation, document, and JSON-string `EpisodeType` inputs, and stores the original source as an Episode.
- **Entity and relation extraction**: Uses an LLM to extract entity declarations, entity summaries, attributes, relations, and relation validity times.
- **Merging and deduplication**: Recalls existing entities and relations during writes, performs entity merging, relation filtering, and semantic deduplication.
- **Graph-structured retrieval**: Searches entity, relation, and Episode collections in parallel, with configurable hybrid ranking, rerank, and BFS expansion for entity/relation results.

[→ Graph Memory API Docs](docs/en/API%20Docs/graph_memory.md)

### **Flexible Storage Backends and Data Migration**

- **Multiple Storage Backends**: Built-in abstract interfaces for four types of storage — KV store (in-memory, Shelve, Redis, database), vector store (Milvus, ChromaDB, GaussVector), relational database (SQLite, PostgreSQL, MySQL, GaussDB), and message store.
- **Versioned Migration**: A complete migration framework supporting SQL schema changes, vector field renaming, KV data updates, message data transformation, and index field operations.
- **Cross-index Migration**: Supports batch migration of memory data between different BaseMemoryIndex instances for smooth storage engine switching.

### **Security and Concurrency Control**

- **AES-GCM Encryption**: Built-in encryption codec supporting encrypted storage of memory data, with automatic encryption protection for sensitive information such as API keys.
- **Distributed Lock**: Distributed lock mechanism based on KV store, ensuring atomicity and consistency of user-level data operations under concurrent scenarios.
- **Scope Isolation**: Supports memory data isolation by scope_id, with each scope independently configurable for LLM, embedding model, and extraction rules.

## Memory Service & OpenClaw Plugin

A ready-to-run memory backend and an OpenClaw plugin that give agents persistent, searchable memory — zero extra code.

### Memory Service

One command to start a local memory engine backed by REST APIs:

- **Memory CRUD** — add messages, update/delete memories, manage key-value variables.
- **Semantic search** — retrieves memories by meaning, not keywords.
- **Zero config** — install `JiuwenMemory[server]`, drop your LLM + embedding keys into `~/.jiuwenmemory/.env`, and run `memory-server`.

```bash
# Install the memory server
pip install JiuwenMemory[server]

# Create config directory and edit .env (see server/.env.example in the source repo for a template)
mkdir -p ~/.jiuwenmemory
cp server/.env.example ~/.jiuwenmemory/.env   # or create manually
vim ~/.jiuwenmemory/.env                       # fill in API keys and backend config

# Start the server
memory-server

# Source-code launch (still works during development)
python -m server.memory_server
```

Configuration and data are stored under `~/.jiuwenmemory/`:

```
~/.jiuwenmemory/
├── .env              ← environment config (LLM / embedding / storage backends)
├── memory_data/      ← data directory (SQLite / ChromaDB, auto-created)
```

### OpenClaw Plugin

Auto-memory for OpenClaw agents — remembers what users said and recalls it before every reply.

- **Recall before reply** — injects relevant context so the agent never starts from scratch.
- **Store after reply** — captures every exchange and extracts structured memories in the background.

[→ Full setup guide](agent-memory-plugin/JiuwenMemory-OpenClaw/README.md)

## Project Structure

```
agent-memory/
├── memory_core/                  # Core memory module
│   ├── long_term_memory.py       # Long-term memory engine entry
│   ├── config/                   # Configuration management
│   │   ├── config.py             # Engine config, scope config, agent config
│   │   └── graph.py              # Graph Memory write and search strategy config
│   ├── manage/                   # Memory management
│   │   ├── index/                # Memory managers
│   │   │   ├── base_memory_manager.py     # Base manager class
│   │   │   ├── fragment_memory_manager.py # Fragment memory manager
│   │   │   ├── variable_manager.py        # Variable manager
│   │   │   ├── summary_manager.py         # Summary manager
│   │   │   └── write_manager.py           # Write manager
│   │   ├── search/               # Search management
│   │   │   └── search_manager.py # Search manager
│   │   ├── update/               # Update detection
│   │   └── mem_model/            # Data models
│   │       ├── memory_unit.py    # Memory unit definitions
│   │       ├── db_model.py       # Database models
│   │       └── sql_db_store.py   # SQL database store
│   ├── process/                  # Memory processing
│   │   ├── extract/              # Memory extraction
│   │   │   ├── generation.py     # Memory generator
│   │   │   ├── long_term_memory_extractor.py  # Long-term memory extractor
│   │   │   └── memory_analyzer.py # Memory analyzer
│   │   ├── dreaming/             # Offline memory consolidation
│   │   │   ├── orchestrator.py   # Background sweep scheduler
│   │   │   ├── source.py         # Session source (reads message store)
│   │   │   ├── sweeper.py        # Compress -> extract -> promote pipeline
│   │   │   └── store.py          # Writes distilled knowledge as memory units
│   │   └── refine/               # Memory refinement
│   ├── graph/                    # Knowledge graph memory
│   │   ├── graph_memory/         # GraphMemory write, search, and state management
│   │   └── extraction/           # Entity/relation extraction models and prompts
│   ├── prompts/                  # Prompt management
│   │   └── prompt_applier.py     # Prompt template engine
│   ├── codec/                    # Encoding/decoding
│   │   └── aes_storage_codec.py  # AES encryption codec
│   ├── migration/                # Data migration
│   │   ├── migration_plan.py     # Migration plan and registry
│   │   ├── migrator/             # Various migrators
│   │   └── operation/            # Migration operation definitions
│   ├── external/                 # External integrations
│   │   ├── provider.py           # MemoryProvider abstract interface
│   │   ├── mem0_provider.py      # Mem0 integration
│   │   ├── agentarts_memory_provider.py  # AgentArts integration
│   │   ├── openjiuwen_memory_provider.py # openJiuwen integration
│   │   └── openviking_memory_provider.py  # openViking integration
│   └── common/                   # Common utilities
│       ├── distributed_lock.py   # Distributed lock
│       └── kv_prefix_registry.py # KV prefix registry
├── foundation/                   # Foundation capabilities
│   ├── llm/                      # LLM invocation
│   │   ├── model.py              # Unified model interface
│   │   └── model_clients/        # Various model clients
│   ├── store/                    # Storage abstractions
│   │   ├── base_kv_store.py      # KV store base class
│   │   ├── base_vector_store.py  # Vector store base class
│   │   ├── base_db_store.py      # Database store base class
│   │   ├── base_message_store.py # Message store base class
│   │   ├── base_memory_index.py  # Memory index base class
│   │   └── graph/                # Graph store abstraction and Milvus implementation
│   ├── prompt/                   # Prompt templates
│   └── tool/                     # Tool definitions
├── retrieval/                    # Retrieval capabilities
│   └── embedding/                # Embedding models
├── common/                       # Common components
│   ├── security/                 # Security utilities
│   ├── logging/                  # Logging management
│   ├── exception/                # Exception handling
│   └── utils/                    # General utilities
├── server/                       # Memory service (FastAPI)
│   ├── __init__.py               # Package init
│   ├── memory_server.py          # HTTP API server (CLI entry point main())
│   ├── store_factory.py          # Storage backend factory
│   └── .env.example              # Environment config template
├── agent-memory-plugin/          # OpenClaw lifecycle plugin
│   ├── lib/                      # Plugin library
│   │   └── openjiuwen-memory-api.js # Memory API client
│   ├── openjiuwen-memory-index.js # Plugin entry point
│   ├── openclaw.plugin.json      # Plugin manifest
│   ├── package.json              # npm package config
│   └── README.md                 # Plugin documentation
└── tests/                        # Test cases
```

## Contributing

We welcome all forms of contributions, including but not limited to:
- Submitting issues and feature suggestions
- Improving documentation
- Submitting code
- Sharing usage experiences

## Open Source License

This project is licensed under the Apache-2.0 License.
