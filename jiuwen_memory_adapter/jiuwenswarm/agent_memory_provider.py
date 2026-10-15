# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMemory 外接记忆 provider —— 适配 JiuwenSwarm ExternalMemoryRail。

把 AgentMemory 的 ``MemoryAPI``（进程内 ``assemble()`` 或 HTTP ``/v1/<verb>``）
适配成 openjiuwen ``MemoryProvider`` ABC 的实现，供 ``ExternalMemoryRail`` /
``JiuwenMemoryRail`` 驱动。

契约来源（2026-10-14 代码核实 + 本地实测，见同目录 ``jiuwen_memory_rail_design.md``）：
- ABC：``openjiuwen/core/memory/external/provider.py`` 的 ``MemoryProvider``
- HTTP 表面：``jiuwen_memory_entry/core/api_contract.py``（F05 对齐后）——
  ``POST /v1/<MemoryAPI 方法名>``（verb 集自动镜像抽象方法集），请求字段与方法
  签名机械镜像（未知字段 400），响应为原返回值的 JSON 序列化：``add`` /
  ``read_md_file`` / ``edit_md_file`` 是**顶层数组**（且无顶层 ``content``，
  需从 ``segments`` 重建）、``search`` 是 ``{items, ...}``、``evolve`` 是裸 job_id。
  **不是** legacy handler（``core/handler.py``）的 ``{ok, op, ...}`` 信封。
- SDK 表面：``jiuwen_memory/api/memory_api.py``——``search``/``list``/``evolve`` 是
  内部 ``asyncio.run`` 的同步方法，须 ``asyncio.to_thread``；``add_async`` 原生协程。
- 参考实现：同目录 ``mem2_provider.py``（== openjiuwen 的 ``jiuwen_memory_provider.py``）。

设计要点（详见 ``jiuwen_memory_rail_design.md`` §6.2）：
- 双模式：``base_url`` 非空 → HTTP（全 async，推荐生产）；否则进程内 ``assemble()``。
- [本地修改 2026-10-17] 进程内默认装配：``config_path`` 缺省时不再要求外部配置文件，
  直接用内置的 agent 场景（F09）默认配置 ``_agent_default_config()``（即
  ``config.serve-doc.yml`` 的代码化副本：md 真源 + shadow 索引 + agent engine +
  agent_llm 抽取，凭证/md root/ES 端点均按该文件字面值写死）。外部 yml 仍可经
  ``config_path`` 覆盖内置默认。
- HTTP scope 用**身份覆盖式**映射（§6.2-D）：服务端授权要求认证身份覆盖 target
  （org/space 硬边界 + user 精确相等），故 org/user 轴由 ``identity_org``/
  ``identity_user`` 占据（dev 认证缺省 local/developer），rail 的 ``user_id``/
  ``agent_id`` 下沉到 session/agent 轴；SDK 模式维持五维直映射（actor==target）。
- ``prefetch`` 与 ``sync_turn`` 共用 ``_prepare`` 剥 TUI 信封（§6.2-E），保证检索
  query 与落盘内容形态一致。
- prefetch 召回统一过滤 ``tier in (semantic, core, procedural)``（§6.2-F），排除
  EPISODIC 对话原文；``agent_memory_search`` 工具不过滤（显式检索允许命中原文）。
  过滤器是 dict DSL，两模式共用（HTTP 直接下发；进程内经 RetrievalQuery 边界
  normalize）。
- HTTP 客户端带熔断器（连续 5 次失败冷却 120s）、分操作超时（读 30s / 写 120s，
  infer=true 的 add 含真实 LLM 抽取）、可选 Bearer 认证（§6.2-B/C）、healthz 探活。
- ``sync_turn`` 经 ``infer="true"`` 同步抽取派生事实（原文落 /messages/ 不建索引）；
  ``on_session_end`` 保留为显式批量 EXTRACT 入口（infer 模式下基本冗余）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.memory.external.provider import MemoryProvider

from jiuwen_memory.api import (
    Channel,
    Context,
    DisclosureLevel,
    EvolveMode,
    Modality,
    RequestSecurityContext,
    assemble,
    install_privacy_filter,
    metadata_for_log,
    redact_for_log,
    scope_for_log,
)
from jiuwen_memory.api import Scope as ApiScope

# [本地修改 2026-06-29] provider 通过 PYTHONPATH 以顶层模块 agent_memory_provider 被 import，
# __name__="agent_memory_provider" 不在 jiuwenswarm/openjiuwen 的 logger 树下，INFO 默认不落文件。
# 挂到 jiuwenswarm logger 树，让 prefetch 召回日志能进 agent_server.log / full.log。
logger = logging.getLogger(
    "jiuwenswarm.agents.harness.common.memory.external.agent_memory_provider"
)
install_privacy_filter(logger)

# --------------------------------------------------------------------------- #
# 工具 schema（OpenAI 风格，Rail 据此自动包 ToolCard，见 §4.8）
# --------------------------------------------------------------------------- #

PROFILE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_profile",
    "description": (
        "Retrieve stored memories about the user — preferences, facts, project context. "
        "Use at conversation start for a full overview."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

READ_MD_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_read_md_file",
    "description": (
        "Read a markdown memory file by filename and return the memory blocks it "
        "contains. md_filename is a basename: USER.md (user profile), MEMORY.md "
        "(long-term memory), or a daily file like 2026-09-30.md. Optionally read "
        "only a line range via offset/limit (1-based lines)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "md_filename": {
                "type": "string",
                "description": "Basename of the md file, e.g. USER.md, MEMORY.md, 2026-09-30.md.",
            },
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to read the cross-project file "
                    "(memory root)."
                ),
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line (default: 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read (default: whole file).",
            },
        },
        "required": ["md_filename"],
    },
}

SEARCH_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_search",
    "description": "Search memories by meaning. Returns relevant memories ranked by similarity.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {
                "type": "integer",
                "description": "Max results (default: 10, max: 50).",
            },
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_conclude",
    "description": (
        # [本地修改 2026-10-17] 原描述含 "corrections"，会把「修改个人信息」类指令
        # 路由到 conclude（append 式写入）而非 edit_md_file（原地替换），产生新旧
        # 矛盾事实共存。改为明确 append-only + 把更正路由到 edit_md_file。
        "Store a NEW durable fact about the user — appends verbatim, no extraction. "
        "It NEVER modifies or removes existing memories. To change or correct what is "
        "already stored, use agent_memory_edit_md_file (read the file first to locate "
        "the exact text)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"conclusion": {"type": "string", "description": "The fact to store."}},
        "required": ["conclusion"],
    },
}

PROCEDURAL_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_procedural",
    "description": (
        "Store a structured procedural memory — a summary of what was done this turn "
        "(goal / steps / outcome). The raw conversation is NOT stored; the LLM summarizes "
        "it into ONE procedural memory record. Use to record how-to / execution history "
        "worth recalling later. No deduplication or context retrieval is performed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The conversation/turn content to summarize into a procedural memory."
                ),
            }
        },
        "required": ["content"],
    },
}

EDIT_MD_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_edit_md_file",
    "description": (
        # [本地修改 2026-10-17] 强化「修改/更正」路由：这是唯一能改动已存记忆的
        # 工具，先用 read_md_file 定位原文再替换。
        "Modify or correct an EXISTING memory — the only way to change what is already "
        "stored: replaces old_text with new_text in place (old_text must appear exactly "
        "once in the file, an error otherwise; the edit supersedes the affected memory "
        "units and returns the new ones). Use agent_memory_read_md_file FIRST to locate "
        "the exact current text — personal info may live in USER.md or a daily file "
        "(YYYY-MM-DD.md)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "md_filename": {
                "type": "string",
                "description": "Basename of the md file, e.g. USER.md, MEMORY.md, 2026-09-30.md.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to replace; must be unique within the file.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text (empty string deletes old_text).",
            },
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to edit the cross-project file "
                    "(memory root)."
                ),
            },
        },
        "required": ["md_filename", "old_text", "new_text"],
    },
}

_MAX_TOP_K = 50
_DEFAULT_PREFETCH_TOP_K = 5
_DEFAULT_SEARCH_TOP_K = 10

# read 工具「offset 起到文件尾」的 line_range end 上界：内核 read_content 对越界
# end clamp 到文件实际行数（local_markdown_store.read_content），大数 end 即无上界。
_UNBOUNDED_END = 10**9

# HTTP 客户端：分操作超时（秒）。infer=true 的 add 触发真实 LLM 抽取 + dedup，
# 写路径超时须长于读路径（§6.2-C，对齐 mem2_provider）。
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 120.0

# HTTP 客户端熔断器：连续 N 次失败后短路冷却，避免服务端不可用时每轮
# prefetch/sync_turn 都等满超时拖慢宿主（§6.2-C）。
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN = 120.0

# prefetch 召回口径（§6.2-F）：只召回提炼产物，排除 EPISODIC 对话原文——
# 原文已由 /messages/ 承载、且会注入整轮对话噪音。显式 search 工具不过滤。
_RECALL_TIERS = ("semantic", "core", "procedural")


def _recall_filters() -> dict[str, Any]:
    """prefetch 召回过滤（dict DSL，HTTP/进程内两模式共用）。

    实测确认（design doc §8.2.1）：``{"tier": {"in": [...]}}`` 被服务端真实执行，
    episodic 原文被正确排除。进程内路径经 ``RetrievalQuery`` 边界的 ``normalize``
    接收同一份 dict DSL。
    """
    return {"tier": {"in": list(_RECALL_TIERS)}}


def _agent_default_config() -> dict[str, Any]:
    """[本地修改 2026-10-17] 进程内默认装配——agent 场景（F09）内核配置。

    ``config.serve-doc.yml`` 的代码化副本（逐段对应，凭证与端点按该文件字面值
    写死），供 ``config_path`` 缺省时的进程内分支直接 ``assemble()``：

    - 真源 = document（md 文件 + sqlite3/sqlite-vec 影子索引，watchdog 监听手改）；
      md 按 memory_class × coords.project 分流（user_profile→USER.md；
      long_term→{project}/MEMORY.md；daily→daily_memory/日期.md）
    - memory_class 由 agent_llm 抽取器（deepseek）同步判定；search 走
      recall_and_get_rank pipeline（文档模式必须），召回统一走 shadow recaller
    - fulltext = elasticsearch（文档模式召回走 shadow FTS5，此端口闲置备用）
    - 每次调用返回**全新字面量**——无共享可变状态，调用方（装配/改写）互不影响

    用户仍可通过 ``config_path`` 传外部 yml/json 覆盖本默认（两级命名空间同构）。
    """
    return {
        "globals": {
            "vector_enabled": True,
            "graph_enabled": False,          # 本地联调先关图，减少装配面
            "rerank_enabled": True,
            "embedder_dim": 1024,
            "chunk_size": 120,
            # 全局唯一 StoreManager 指名（F07）
            "store_manager": "default",
            # 记忆写入文档开关（F07）：true 时真源写 md 文档 + 影子索引（不写 KV）
            "write_document": True,
            # 文档看门狗（F07 §12）：监听 md 漂移并同步影子索引
            "watch_document": True,
            # openai llm/embedder 连接参数（ComponentConfig.get 回退到 globals 命中）
            "llm_base_url": os.getenv("JIUWEN_MEMORY_BASE_URL", "https://api.deepseek.com"),
            "llm_api_key": os.getenv("JIUWEN_MEMORY_API_KEY", ""),
            "llm_model": os.getenv("JIUWEN_MEMORY_MODEL", "deepseek-v4-pro"),
            "llm_temperature": float(os.getenv("JIUWEN_MEMORY_TEMPERATURE", "0.0")),
            "llm_max_tokens": int(os.getenv("JIUWEN_MEMORY_MAX_TOKENS", "4096")),
            # 只填到 /v1；SDK 会自动拼 /embeddings
            "embedder_base_url": os.getenv("EMBED_API_BASE", "https://api.siliconflow.cn/v1"),
            "embedder_api_key": os.getenv("EMBED_API_KEY", ""),
            "embedder_model": os.getenv("EMBED_MODEL", "BAAI/bge-m3"),
            "embedder_max_batch": int(os.getenv("EMBED_MAX_BATCH", "64")),
        },
        # ---- 存储 -------------------------------------------------------- #
        # document 模式下 KV 仍保留：vector chunk_tracking 硬依赖它
        "kv_store": {"default": "memory"},
        # L2 向量索引，in_memory 重启即失（文档模式真索引在 shadow）
        "vector_store": {"default": "memory"},
        # 文档模式召回走 shadow FTS5，此端口闲置备用
        "fulltext_store": {"default": {"target": "elasticsearch", "params": {
            "hosts": ["http://1.94.231.141:8537"],
            "index": "agent_memory_fulltext",
            "username": "elastic",
            "password": "Huawei@234",
            "text_field": "text",
            "refresh": "false",
        }}},
        # graph_enabled=false 不实际用，留默认避免装配报错
        "graph_store": {"default": "memory"},
        # md 真源 root
        "markdown_store": {"default": {"target": "local", "params": {
            "root": "e:/02_memory/agent-memory/_md_store",
        }}},
        # 影子索引（document 真源的机器索引：sqlite3 + sqlite-vec 复合）
        "shadow_index": {"default": {"target": "sqlite", "params": {"embedder": "default"}}},
        # 文档看门狗：md 保存后 debounce 毫秒再同步影子索引
        "watchdog": {"default": {"target": "watchdog", "params": {"debounce_ms": 2000}}},
        # 全局唯一 StoreManager（F07）：文档模式召回统一走 shadow_recaller
        "store_manager": {"default": {"target": "composite", "params": {
            "domain_stores": {"default": {
                "kv_store": "default",
                "shadow_recaller": "shadow",
                # 召回+物化在 storage 内一次完成（文档模式必须走此 pipeline）
                "preferred_retrieval_pipeline": "recall_and_get_rank",
            }},
        }}},
        # ---- 共享插件 ------------------------------------------------------ #
        # jieba 分词（中文召回，whitespace 会把整句当一个 token）
        "tokenizer": {"default": {"target": "jieba"}},
        "embedder": {"default": {"target": "openai", "params": {"tokenizer": "default"}}},
        "chunker": {"default": "fixed_window"},
        "feature_extractor": {"default": {"target": "keyword", "params": {"tokenizer": "default"}}},
        "llm": {"default": "openai"},
        "reranker": {"default": {"target": "overlap", "params": {"tokenizer": "default"}}},
        "normalizer": {"default": "passthrough"},
        "audit": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        # ---- 检索 ---------------------------------------------------------- #
        # 文档模式唯一召回通道（shadow_search_fulltext + search_vector 复合算子）
        "recaller": {"shadow": {"target": "shadow"}},
        "query_parser": {"default": {"target": "simple", "params": {
            "tokenizer": "default",
            "embedder": "default",
            "llm": "default",
            "feature_extractor": "default",
            "sanitize_enabled": True,
            "sanitize_strip_code": False,
        }}},
        "fuser": {"default": "rrf"},
        "discloser": {"default": "truncating"},
        "retriever": {"default": {"target": "pipeline", "params": {
            "reranker": "default",
            "query_parser": "default",
            "fuser": "default",
            "discloser": "default",
            "over_fetch_factor": 4,
            "over_fetch_floor": 60,
            "recall_max": 100,
            "rerank_max": 60,
            "min_score": 0.0,
            "min_score_ratio": 0.0,
            "min_score_ratio_uncalibrated": 0.0,
            "min_results": 0,
        }}},
        # ---- 构建 ---------------------------------------------------------- #
        # agent 场景（F09）：prompt user↔agent 重定向 + cn 指令面，抽取时同步判定
        # memory_class 三类（md 落盘分流键），无独立 fallback 路径
        "extractor": {"default": {"target": "agent_llm",
                                  "params": {"llm": "default", "extractor_language": "cn"}}},
        "abstractor": {"default": "concat"},
        "associator": {"default": {"target": "keyword", "params": {"feature_extractor": "default"}}},
        "classifier": {"default": "llm"},
        # 文档模式（write_document=true）：IndexBuilder 全委托 domain_store.add 文档分流
        "constructor": {"default": {"target": "document", "params": {"domain_store": "default"}}},
        # 文档模式去重（F08）：两路并跑（shadow FTS5 + vec）+ embedder cosine 精排
        "dedup": {"default": {"target": "shadow", "params": {"embedder": "default"}}},
        "evolver": {"default": {"target": "orchestrating", "params": {
            "extractor": "default", "abstractor": "default", "associator": "default",
            "index_builder": "default", "message_store": "default",
            "dedup": "default", "llm": "default",
        }}},
        # ---- 摄取 ---------------------------------------------------------- #
        "ingestor": {"default": {"target": "simple", "params": {"normalizer": "default"}}},
        # ---- 控制 ---------------------------------------------------------- #
        # agent 场景（F09）：多空间无硬限制、纯文本模态。params 必须带全套具名引用——
        # 裸 target 会顶掉内置 engine.default.params，dep 全走匿名缺省一路劣化
        "engine": {"default": {"target": "agent", "params": {
            "ingestor": "default", "index_builder": "default", "retriever": "default",
            "scheduler": "default", "evolver": "default", "lifecycle": "default",
            "job_factory": "default",
        }}},
        "job_factory": {"default": {"target": "default", "params": {
            "evolver": "default", "lifecycle": "default", "index_builder": "default",
            "llm": "default",
            "middle_max_fetch": 100, "middle_batch_size": 10, "middle_concurrency": 4,
        }}},
        "scheduler": {"default": {"target": "in_process", "params": {}}},
        "lifecycle": {"default": {"target": "kv", "params": {"policy": "default"}}},
        "policy": {"default": "dict"},
        "governor": {"default": {"target": "in_memory", "params": {"audit": "default"}}},
        "space": {"default": {"target": "kv", "params": {}}},
        "config_source": {"default": "yaml_defaults"},
    }


class AgentMemoryMemoryProvider(MemoryProvider):
    """AgentMemory 适配 ``MemoryProvider`` ABC。

    构造参数（与 jiuwenswarm ``memory.external.agent_memory`` 子段对齐）：

    - ``base_url``：AgentMemory HTTP 服务地址（如 ``http://127.0.0.1:8137``）。
      非空 → HTTP 模式（推荐生产）；空 → 进程内模式。
    - ``config_path``：进程内模式的 AgentMemory 配置文件路径（YAML/JSON），
      **可选**——[本地修改 2026-10-17] 缺省时用内置 agent 场景默认装配
      ``_agent_default_config()``（F09 文档真源：md + shadow 索引 + agent_llm
      抽取，即 serve-doc 的代码化副本）；外部 yml 传入则覆盖内置默认。
    - ``user_id``/``agent_id``：兼容常见记忆层隔离键（默认约定一致）。
    - ``security_provider``：进程内模式的可信身份来源——由可信 composition root
      注入的 ``() -> RequestSecurityContext``（P1-2 角色感知授权）。身份不由本
      provider 从 ``user_id``/``scope_id`` 自述（那些只是业务 target 维度，不是
      认证结论）；进程内直连缺少它时 initialize 会 fail-closed。
    - ``api_key``：HTTP 模式可选 Bearer 凭据（服务端 ``--auth-mode required`` 必需）。
    - ``identity_org``/``identity_user``：HTTP 模式服务端认证身份的 org/user
      （身份覆盖式 scope 的身份轴，§6.2-D）。缺省对齐 dev 认证器的固定身份
      ``local``/``developer``；生产配真实认证身份。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        config_path: str | None = None,
        user_id: str = "jiuwenswarm-user",
        agent_id: str = "jiuwenswarm",
        space: str = "",
        security_provider: Any = None,
        api_key: str = "",
        identity_org: str = "local",
        identity_user: str = "developer",
        **_kwargs: Any,
    ) -> None:
        self._base_url = base_url or ""
        self._config_path = config_path
        self._user_id = user_id
        self._agent_id = agent_id
        self._space = space
        # 进程内模式的可信身份来源：由可信 composition root 注入的
        # ``() -> RequestSecurityContext``（P1-2）。身份不由本 provider 从
        # user_id/scope_id 自述——那些只是业务 target 维度，不是认证结论。
        self._security_provider = security_provider
        self._api_key = api_key
        self._identity_org = identity_org or "local"
        self._identity_user = identity_user or "developer"

        # Rail 在 before_invoke 首次调 initialize 时传入，覆盖构造默认
        self._scope_id: str = "__default__"
        self._session_id: str = "__default__"

        self._client: _AgentMemoryClient | None = None
        self._initialized = False

    # -- MemoryProvider: 元信息 ---------------------------------------------- #

    @property
    def name(self) -> str:
        return "agent_memory"

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # -- TUI 信封剥离（§6.2-E：prefetch / sync_turn 共用统一入口） ------------- #

    # jiuwenswarm 把用户输入包成 TUI 信封再送进 agent（见 interface.py:498）：
    #   "你收到一条消息：\n{source,timezone,timestamp,content,...json}"
    #   / "You receive a new message:\n{...json}"
    # rail 的 _resolve_user_text_for_memory 老实取了整段信封当 query 传给 prefetch，
    # 导致 AgentMemory 用带壳文本检索，命中对话原文而非语义事实。
    _TUI_ENVELOPE_PREFIXES = (
        "你收到一条消息：\n",
        "你收到一条消息，对于查询类任务必须输出查询到的内容，不要只回复确认，不要记录到memory：\n",
        "You receive a new message:\n",
        "You receive a new message. For query tasks, you must output the queried content"
        "—don't just reply with confirmation, don't record to memory:\n",
    )

    @classmethod
    def _strip_tui_envelope(cls, query: str) -> str:
        """若 query 是 jiuwenswarm TUI 信封，剥出纯 content；否则原样返回。"""
        if not query:
            return query
        for prefix in cls._TUI_ENVELOPE_PREFIXES:
            if query.startswith(prefix):
                rest = query[len(prefix) :].lstrip()
                try:
                    data = json.loads(rest)
                except json.JSONDecodeError:
                    # 前缀命中但非合法 json（可能 interaction_prefix 拼在前），回退原 query
                    return query
                if isinstance(data, dict):
                    content = data.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                return query
        return query

    def _prepare(self, text: str) -> str:
        """统一剥壳入口：检索 query 与落盘内容共用，保证两者形态一致（§6.2-E）。"""
        return self._strip_tui_envelope(text) if text else text

    @staticmethod
    def _parse_line_range(args: dict) -> tuple[int, int] | None:
        """read 工具 ``offset``/``limit`` → 内核 ``line_range``（1-based 闭区间，F08）。

        offset 缺省 1；limit 缺省 → 无上界（end 传 ``_UNBOUNDED_END``，内核 clamp
        到文件实际行数）；两者都缺省 → None（整文件）。``limit <= 0`` 得
        ``end < start``，内核切出空串（读 0 行），如实返回空。
        """
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is None and limit is None:
            return None
        start = max(int(offset or 1), 1)
        if limit is None:
            return (start, _UNBOUNDED_END)
        return (start, start + int(limit) - 1)

    def is_available(self) -> bool:
        """配置就绪探测（契约要求无网络调用）。

        HTTP 模式：``base_url`` 配置就绪即可。进程内模式还需注入了可信
        security provider——缺少它 initialize 会 fail-closed，is_available 如实
        报不可用（config 可由外部文件或内置 agent 默认装配，二选一必有）。
        不真发请求（避免网络）。
        """
        if self._base_url:
            return True
        return self._security_provider is not None

    async def initialize(self, **kwargs: Any) -> None:
        """Rail ``before_invoke`` 首次调，传 ``user_id``/``scope_id``/``session_id``。

        ``scope_id`` 只映射到 ``org``；``space`` 只认显式传入，不从 ``scope_id`` 猜测。
        再次调用只更新绑定维度，不重建客户端（同一进程内内核可切换 space）。
        """
        self._user_id = kwargs.get("user_id") or self._user_id
        self._scope_id = kwargs.get("scope_id", "__default__")
        self._session_id = kwargs.get("session_id", "__default__")
        # 兼容约定：agent_id 可由 kwargs 覆盖
        if kwargs.get("agent_id"):
            self._agent_id = kwargs["agent_id"]
        if "space" in kwargs:
            self._space = kwargs.get("space") or ""

        if self._client is None:
            self._client = _build_client(
                self._base_url,
                self._config_path,
                security_provider=self._security_provider,
                api_key=self._api_key,
                identity_org=self._identity_org,
                identity_user=self._identity_user,
            )
        await self._client.initialize()
        self._initialized = True
        logger.info(
            "[AgentMemoryMemoryProvider] initialized (mode=%s, config=%s, scope=%s)",
            "http" if self._base_url else "in-process",
            self._base_url or self._config_path or "builtin-agent-default",
            scope_for_log(self.bound_scope()),
        )

    # -- MemoryProvider: 工具声明 + 调度 -------------------------------------- #

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI 风格 schema；Rail 自动包成 ToolCard（§4.8）。

        [本地修改 2026-10-17] ``agent_memory_profile`` / ``agent_memory_procedural``
        **不在注册列表**——LLM 看不到、不会主动调用；但两者的 schema 与
        ``handle_tool_call`` 实现完整保留，供内部直调（测试 / 排查 / 后续
        重新注册时直接加回列表即可）。
        """
        schemas = [SEARCH_SCHEMA, CONCLUDE_SCHEMA, READ_MD_SCHEMA, EDIT_MD_SCHEMA]
        logger.info(
            "[AgentMemoryMemoryProvider] get_tool_schemas CALLED -> returning %d schemas: %s",
            len(schemas),
            [s.get("name") for s in schemas],
        )
        return schemas

    async def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """LLM 调工具时触发；返 JSON 字符串（Rail 会 ``json.loads``）。"""
        logger.info(
            "[AgentMemoryMemoryProvider] handle_tool_call CALLED tool=%s args=%s",
            tool_name,
            metadata_for_log(args),
        )
        if self._client is None:
            return json.dumps({"error": "provider not initialized"})
        if self._client.is_unavailable():
            return json.dumps(
                {"error": "AgentMemory temporarily unavailable (repeated failures); will retry."}
            )
        scope = self.bound_scope()

        try:
            # -- 未注册工具（不在 get_tool_schemas，LLM 不可见；实现保留供内部直调）-- #

            if tool_name == "agent_memory_profile":
                items = await self._client.list_semantic(scope)
                if not items:
                    logger.info("[AgentMemoryMemoryProvider] agent_memory_profile -> no memories")
                    return json.dumps({"result": "No memories stored yet."})
                lines = [it["content"] for it in items if it.get("content")]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_profile -> count=%d", len(lines)
                )
                for idx, line in enumerate(lines):
                    logger.info(
                        "[AgentMemoryMemoryProvider] profile hit[%d]: %s",
                        idx,
                        redact_for_log(line),
                    )
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})

            if tool_name == "agent_memory_read_md_file":
                md_filename = str(args.get("md_filename") or "")
                if not md_filename:
                    return json.dumps({"error": "Missing required parameter: md_filename"})
                project = str(args.get("project") or "")
                line_range = self._parse_line_range(args)
                # F08：按 md_filename + 行范围读 md 文件，返回命中的 MemoryUnit 块。
                # 文件不存在/行范围无完整块 → 空列表（内核 read_content 容错空串）；
                # 非文档模式服务端抛 ValidationError（经 client 透出）。
                units = await self._client.read_md_file(
                    md_filename, scope, project=project, line_range=line_range
                )
                if units is None:
                    return json.dumps(
                        {"error": "AgentMemory read_md_file failed (server unavailable)."}
                    )
                if not units:
                    logger.info(
                        "[AgentMemoryMemoryProvider] agent_memory_read_md_file file=%s "
                        "-> no blocks",
                        md_filename,
                    )
                    return json.dumps(
                        {"result": f"No memory blocks found in {md_filename}."}
                    )
                blocks = [
                    {
                        "item_id": it.get("item_id", ""),
                        "tier": it.get("tier", ""),
                        "memory": it.get("content", ""),
                    }
                    for it in units
                ]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_read_md_file file=%s -> count=%d",
                    md_filename,
                    len(blocks),
                )
                for idx, blk in enumerate(blocks):
                    logger.info(
                        "[AgentMemoryMemoryProvider] read_md hit[%d]: %s",
                        idx,
                        redact_for_log(blk.get("memory")),
                    )
                return json.dumps(
                    {"md_filename": md_filename, "count": len(blocks), "blocks": blocks}
                )

            if tool_name == "agent_memory_search":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"error": "Missing required parameter: query"})
                # [本地修改 2026-10-17] LLM 可能显式传 "top_k": null——key 存在时
                # dict.get 的缺省值不生效，int(None) 直接 TypeError。用 or 同时覆盖
                # 「key 缺省」与「显式 null」（top_k=0 无意义，同样回落缺省）。
                top_k = min(int(args.get("top_k") or _DEFAULT_SEARCH_TOP_K), _MAX_TOP_K)
                # 显式检索不过滤 tier（§6.2-F）：LLM 主动检索允许命中 EPISODIC 原文。
                items = await self._client.search(query, scope, top_k=top_k)
                if not items:
                    logger.info(
                        "[AgentMemoryMemoryProvider] agent_memory_search query=%s -> no relevant",
                        redact_for_log(query),
                    )
                    return json.dumps({"result": "No relevant memories found."})
                payload = [
                    {"memory": it.get("content", ""), "score": it.get("score", 0)} for it in items
                ]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_search query=%s -> count=%d",
                    redact_for_log(query),
                    len(payload),
                )
                for idx, p in enumerate(payload):
                    logger.info(
                        "[AgentMemoryMemoryProvider] search hit[%d] score=%s: %s",
                        idx,
                        p.get("score"),
                        redact_for_log(p.get("memory")),
                    )
                return json.dumps({"results": payload, "count": len(payload)})

            if tool_name == "agent_memory_conclude":
                conclusion = args.get("conclusion", "")
                if not conclusion:
                    return json.dumps({"error": "Missing required parameter: conclusion"})
                # 原样存（对齐常见记忆层 infer=False）；add 会走缺省路径原文落盘 + 建索引。
                data = await self._client.add(conclusion, scope, tags=["conclude"])
                if data is None:
                    return json.dumps({"error": "AgentMemory add failed (server unavailable)."})
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_conclude stored=%s",
                    redact_for_log(conclusion),
                )
                if data.item_id is None:
                    return json.dumps({"result": "Fact stored (deduped, no new unit)."})
                return json.dumps({"result": "Fact stored.", "item_id": data.item_id})

            # -- 未注册工具（不在 get_tool_schemas，LLM 不可见；实现保留供内部直调）-- #

            if tool_name == "agent_memory_procedural":
                content = args.get("content", "")
                if not content:
                    return json.dumps({"error": "Missing required parameter: content"})
                # 过程记忆：经 add(procedural=true) 触发 engine 的 procedural 分支——
                # 原文不落 KV，evolver 让 extractor 把本轮汇总成 1 条 PROCEDURAL 执行历史，
                # 落 /memory/ 建索引；不走去重、不收集 context（见 F02「过程记忆抽取」）。
                # 需配 extractor:llm 才真汇总（默认 keyword 降级为原文原样存 1 条 PROCEDURAL）。
                data = await self._client.add(
                    content, scope, system_metadata={"procedural": "true"}
                )
                if data is None:
                    return json.dumps({"error": "AgentMemory add failed (server unavailable)."})
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_procedural summarized=%s item_id=%s",
                    redact_for_log(content),
                    data.item_id,
                )
                # add 返回 item_id=None：extractor 产空（LLM 返回不可解析/未产出候选）→
                # 未持久化任何记忆。不可报成功（false success）——evolver 吞掉 LLM 失败
                # 返回空 EvolveResult，需如实告知调用方。content 已在上文校验非空。
                if not data.item_id:
                    return json.dumps(
                        {
                            "error": "Procedural memory not stored: extractor produced nothing "
                            "(LLM returned unparseable content or no candidates).",
                        }
                    )
                return json.dumps({"result": "Procedural memory stored.", "item_id": data.item_id})

            if tool_name == "agent_memory_edit_md_file":
                md_filename = str(args.get("md_filename") or "")
                old_text = args.get("old_text", "")
                new_text = args.get("new_text", "")
                if not md_filename:
                    return json.dumps({"error": "Missing required parameter: md_filename"})
                if not old_text:
                    return json.dumps({"error": "Missing required parameter: old_text"})
                # new_text 允许空串（old_text → "" 即删除该片段）。
                project = str(args.get("project") or "")
                # F08：按 md_filename + old_text 文件内唯一替换，继承式 SUPERSEDE——
                # 返回受影响的**新** unit（旧版已失效）。old_text 不存在/不唯一由
                # 内核抛 NotFoundError/ValidationError（经 client 透出给 LLM）。
                units = await self._client.edit_md_file(
                    md_filename, scope, old_text, new_text, project=project
                )
                if units is None:
                    return json.dumps(
                        {"error": "AgentMemory edit_md_file failed (server unavailable)."}
                    )
                blocks = [
                    {
                        "item_id": it.get("item_id", ""),
                        "tier": it.get("tier", ""),
                        "memory": it.get("content", ""),
                    }
                    for it in units
                ]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_edit_md_file file=%s -> count=%d",
                    md_filename,
                    len(blocks),
                )
                return json.dumps(
                    {
                        "result": "Edited.",
                        "md_filename": md_filename,
                        "count": len(blocks),
                        "units": blocks,
                    }
                )

            logger.info("[AgentMemoryMemoryProvider] handle_tool_call unknown tool=%s", tool_name)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] handle_tool_call '%s' failed: error_type=%s",
                tool_name,
                type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})

    async def prefetch(self, query: str, **kwargs: Any) -> str:
        """返 Markdown 字符串，Rail 包 ``<memory-context>`` 注入提示词。"""
        logger.info(
            "[AgentMemoryMemoryProvider] prefetch CALLED query=%s client=%s",
            redact_for_log(query),
            bool(self._client),
        )
        if not query or self._client is None:
            return ""
        # §6.2-E：剥 TUI 信封，用纯 content 检索，避免命中对话原文壳。
        search_query = self._prepare(query)
        if search_query != query:
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch stripped envelope: %s -> %s",
                redact_for_log(query),
                redact_for_log(search_query),
            )
        # [本地修改 2026-10-17] 同 agent_memory_search：null 防御（or 覆盖缺省与显式 null）。
        top_k = min(int(kwargs.get("top_k") or _DEFAULT_PREFETCH_TOP_K), _MAX_TOP_K)
        try:
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch before search scope=%s top_k=%d",
                scope_for_log(self.bound_scope()),
                top_k,
            )
            # §6.2-F：prefetch 只召回提炼产物（semantic/core/procedural）。
            items = await self._client.search(
                search_query,
                self.bound_scope(),
                top_k=top_k,
                filters=_recall_filters(),
            )
            logger.info("[AgentMemoryMemoryProvider] prefetch after search items=%d", len(items))
            lines = [it.get("content", "") for it in items if it.get("content")]
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch search_query=%s scope=%s "
                "top_k=%d recalled=%d",
                redact_for_log(search_query),
                scope_for_log(self.bound_scope()),
                top_k,
                len(lines),
            )
            for idx, line in enumerate(lines):
                logger.info(
                    "[AgentMemoryMemoryProvider] prefetch hit[%d]: %s",
                    idx,
                    redact_for_log(line),
                )
            return (
                ("## AgentMemory Memory\n" + "\n".join(f"- {line}" for line in lines))
                if lines
                else ""
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] prefetch failed: error_type=%s",
                type(exc).__name__,
                exc_info=True,
            )
            return ""

    # -- MemoryProvider: 每轮回写（after_invoke，非心跳/cron） --------------- #

    async def sync_turn(self, user_msg: str, assistant_msg: str, **kwargs: Any) -> None:
        """存本轮对话并**同步抽取事实**（对齐常见记忆层 ``add(infer=True)``）。

        传 ``system_metadata={"infer": "true"}`` 给 AgentMemory ``add``：hot path 同步调
        Extractor 从 ``user: ...\\nassistant: ...`` 抽取派生事实并建索引，**原文
        落 KV 真源但不建索引**，且不再提交 background EXTRACT（避免每轮全量重扫的
        EXTRACT 风暴）。需配 ``extractor:llm``+``llm:openai`` 才真抽取；默认
        ``keyword`` extractor 走切分占位。

        §6.2-E：user_msg 先经 ``_prepare`` 剥 TUI 信封——与 prefetch 共用同一入口，
        保证检索 query 与落盘内容形态一致（此前 prefetch 剥壳、sync_turn 存壳，
        记忆里会存进带壳信封文本）。
        """
        if not user_msg or not assistant_msg or self._client is None:
            return
        user_msg = self._prepare(user_msg)
        if not user_msg:
            return
        content = f"user: {user_msg}\nassistant: {assistant_msg}"
        system_metadata = {"infer": "true"}
        logger.info(
            "[AgentMemoryMemoryProvider] sync_turn add system_metadata=%s scope=%s content_len=%d",
            metadata_for_log(system_metadata),
            scope_for_log(self.bound_scope()),
            len(content),
        )
        try:
            data = await self._client.add(
                content,
                self.bound_scope(),
                tags=["conversation"],
                system_metadata=system_metadata,
            )
            if data is None:
                logger.warning(
                    "[AgentMemoryMemoryProvider] sync_turn add failed "
                    "(client degraded, breaker recorded)"
                )
                return
            logger.info(
                "[AgentMemoryMemoryProvider] sync_turn add system_metadata=%s done item_id=%s",
                metadata_for_log(system_metadata),
                data.item_id,
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] sync_turn add failed: error_type=%s",
                type(exc).__name__,
            )

    # -- MemoryProvider: 静态提示词 + 生命周期（非抽象，有默认） -------------- #

    def system_prompt_block(self) -> str:
        # [本地修改 2026-10-17] 显式路由规则：新增→conclude（append-only），
        # 修改/更正→read_md_file 定位 + edit_md_file 原地替换。个人信息可能在
        # USER.md 也可能在 daily 文件（YYYY-MM-DD.md），修改前先读定位。
        return (
            "# AgentMemory Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Tool routing: agent_memory_search finds memories by meaning. "
            "agent_memory_conclude ONLY appends a brand-new fact — it never modifies "
            "existing memories. To CHANGE or CORRECT a stored memory, first locate it "
            "with agent_memory_read_md_file (USER.md / MEMORY.md / daily YYYY-MM-DD.md — "
            "personal info may be in a daily file), then agent_memory_edit_md_file to "
            "replace it in place."
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._initialized = False

    async def on_session_end(self, messages: list[dict[str, Any]] | None = None) -> None:
        """会话结束时批量抽取一次（infer 模式下基本冗余，保留为显式入口）。

        触发 ``evolve(EXTRACT)`` 让 AgentMemory Extractor 从累积原文抽取事实。
        需配 ``extractor:llm``+``llm:openai`` 才真抽取（默认 keyword 空转）。
        """
        if self._client is None:
            return
        logger.info(
            "[AgentMemoryMemoryProvider] on_session_end CALLED scope=%s, triggering EXTRACT",
            scope_for_log(self.bound_scope()),
        )
        try:
            await self._client.evolve_extract(self.bound_scope())
            logger.info("[AgentMemoryMemoryProvider] session-end EXTRACT done")
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] on_session_end EXTRACT failed: error_type=%s",
                type(exc).__name__,
            )

    # -- 内部：Scope 映射 ---------------------------------------------------- #

    def bound_scope(self):
        """Rail 入参 → 轻量 _Scope（SDK 模式的五维直映射）。

        SDK 映射（§4.5）：
        - ``user_id`` → ``.user``
        - ``agent_id`` → ``.agent``
        - ``session_id`` → ``.session``（空则跨会话共享，显式传才隔离）
        - ``scope_id`` → ``.org``（作 tenant）
        - ``space`` → ``.space``（只认显式传入，不从 ``scope_id`` 猜测）
        identity = target（actor==target，单租户，与 HTTP surface 一致）

        HTTP 模式不使用本映射的 org/user 轴——服务端授权要求认证身份覆盖
        target（org 硬边界），``_HttpClient._scope_payload`` 会做身份覆盖式
        重映射（§6.2-D）：org/user ← identity 配置，agent/session ← 语义轴下沉。
        """
        return _Scope(
            org=self._scope_id,
            space=self._space,
            user=self._user_id,
            agent=self._agent_id,
            session=self._session_id,
        )


# --------------------------------------------------------------------------- #
# AgentMemory 客户端协议（HTTP / 进程内两种实现）
# --------------------------------------------------------------------------- #


@dataclass
class _Scope:
    """轻量 Scope（org/space/user/agent/session 五维），不依赖 AgentMemory api 包。

    HTTP 模式直接用它的属性拼 payload；进程内模式在 _InProcessClient 里
    转成 api.Scope。这样 HTTP 模式无需 AgentMemory src 在 sys.path。
    """

    org: str = ""
    space: str = ""
    user: str = ""
    agent: str = ""
    session: str = ""


@dataclass
class _AddResult:
    """add 的统一结果（两模式共用）。

    ``item_id is None`` 表示未落任何新记忆——infer/procedural 路径下 extractor
    产空或派生记忆全量 dedup 是**合法结果**，与传输失败（整个 add 返回 None）
    严格区分，调用方不得混淆（§6.2-A）。
    """

    item_id: str | None
    content: str = ""
    tier: str = ""


class AgentMemoryClientError(RuntimeError):
    """服务端业务拒绝（HTTP 4xx）——old_text 不唯一、非文档模式等。

    对显式工具调用（read_md_file / edit_md_file）有信号价值，须透出给 LLM
    （``handle_tool_call`` 兜底转 ``{"error": ...}``），区别于传输失败降级
    None；不计入熔断器——业务拒绝不代表服务不可用。
    """


class _AgentMemoryClient:
    """AgentMemory 记忆操作的 async 客户端协议。

    失败语义（对齐 mem2_provider）：传输/HTTP 失败时**降级返回**（add → None、
    search → []）并记入熔断器（HTTP 模式），不向调用方抛异常——prefetch/
    sync_turn 的既有 try/except 兜底不变，但主要失败路径在此处已收敛。

    md 文件操作（F08 read_md_file / edit_md_file）例外：**业务错误向上抛**——
    old_text 不唯一 / 文件模式未开等 4xx 对显式工具调用有信号价值，
    ``handle_tool_call`` 外层兜底转 ``{"error": ...}`` 透出给 LLM；只有传输
    失败（HTTP 模式）降级返回 None，与 add 同口径。
    """

    async def initialize(self) -> None:
        raise NotImplementedError

    async def add(
        self,
        content: str,
        scope,
        *,
        tags: list[str] | None = None,
        system_metadata: dict[str, str] | None = None,
        user_metadata: dict[str, str] | None = None,
    ) -> _AddResult | None:
        raise NotImplementedError

    async def search(
        self,
        query: str,
        scope,
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        """列出 scope 下已建索引的记忆单元。

        [本地修改 2026-10-17] profile 工具下线后暂无调用方，保留备用
        （进程内 space 隔离排查 / 后续 profile 类工具复用）。
        """
        raise NotImplementedError

    async def read_md_file(
        self,
        md_filename: str,
        scope,
        *,
        project: str = "",
        line_range: tuple[int, int] | None = None,
    ) -> list[dict[str, Any]] | None:
        """读 md 文件，返回命中块对应的 unit 列表（F08）。

        文件不存在/行范围无完整块 → ``[]``（合法空结果）；传输失败 → None；
        业务错误（非文档模式等）向上抛。
        """
        raise NotImplementedError

    async def edit_md_file(
        self,
        md_filename: str,
        scope,
        old_text: str,
        new_text: str,
        *,
        project: str = "",
    ) -> list[dict[str, Any]] | None:
        """md 文件内唯一替换，返回受影响的**新** unit 列表（F08）。

        传输失败 → None；业务错误（old_text 不存在/不唯一、非文档模式）向上抛。
        """
        raise NotImplementedError

    async def evolve_extract(self, scope) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def is_unavailable(self) -> bool:
        return False


def _build_client(
    base_url: str,
    config_path: str | None,
    *,
    security_provider: Any = None,
    api_key: str = "",
    identity_org: str = "local",
    identity_user: str = "developer",
) -> _AgentMemoryClient:
    """按优先级构建客户端：HTTP > 外部配置文件 > 内置 agent 默认装配。

    [本地修改 2026-10-17] 进程内分支缺省不再回落 ``assemble(None)``（echo LLM +
    内存 KV 的通用内核缺省），而是用 ``_agent_default_config()``（agent 场景 F09
    文档真源装配，serve-doc 的代码化副本）。

    进程内分支统一携带 ``security_provider``——身份由可信 composition root 注入
    而非从业务 scope 自述（P1-2）；HTTP 分支的身份由 API Key 认证决定，与本参数
    无关（``api_key``/``identity_*`` 只作用于 HTTP 客户端）。
    """
    if base_url:
        return _HttpClient(
            base_url,
            api_key=api_key,
            identity_org=identity_org,
            identity_user=identity_user,
        )
    if config_path:
        return _InProcessClient(config_path, security_provider=security_provider)
    return _InProcessClient(
        None, config_dict=_agent_default_config(), security_provider=security_provider
    )


# --------------------------------------------------------------------------- #
# HTTP 客户端 —— api_contract 表面（F05 对齐后，2026-10-14 代码核实 + 实测）
# --------------------------------------------------------------------------- #


class _HttpClient(_AgentMemoryClient):
    """经 AgentMemory HTTP surface（``POST /v1/<MemoryAPI 方法名>``）调用的 async 客户端。

    契约（``jiuwen_memory_entry/core/api_contract.py``，实测见 design doc §8.2.1）：
    - 请求字段与 ``MemoryAPI`` 方法签名机械镜像，未知字段 400（无旧别名）；
    - ``scope`` 是嵌套对象 ``{org, space, user, agent, session}``（不是扁平键）；
    - ``search`` 用 ``top_k``（不是 ``k``）、必带 ``context`` 包装；
    - ``add`` 响应是**顶层数组**（``list[MemoryUnit]`` 序列化），序列化**不含**
      顶层 ``content``（property 不进 ``dataclasses.fields``），需从 ``segments``
      按内核方式（换行拼接）重建；空数组 = infer 全量 dedup，是合法结果；
    - ``search`` 响应是 ``{items, trajectory, errors}``（键是 ``items`` 不是 ``hits``）；
    - ``evolve`` 响应是裸 job_id 字符串（无 ``{ok, op}`` 信封）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        identity_org: str = "local",
        identity_user: str = "developer",
        read_timeout: float = _READ_TIMEOUT,
        write_timeout: float = _WRITE_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._api_key = api_key
        # 服务端认证身份（org/user）。授权模型要求身份覆盖 target scope（org 是
        # 硬边界），故这两轴不能承载 per-call 租户/用户数据——必须与服务端认证
        # 的身份一致。缺省对齐 dev 认证器固定身份；生产指向真实认证身份（§6.2-D）。
        self._identity_org = identity_org or "local"
        self._identity_user = identity_user or "developer"
        self._read_timeout = read_timeout
        self._write_timeout = max(write_timeout, _WRITE_TIMEOUT)
        self._http: Any | None = None
        self._is_initialized = False

        # 熔断器状态（读/写路径共享，服务端不可用时两边一起短路）
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    # -- 熔断器 ------------------------------------------------------------- #

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN
            logger.warning(
                "[AgentMemoryMemoryProvider/http] circuit breaker opened after %d failures, "
                "cooldown %ss",
                self._consecutive_failures,
                _BREAKER_COOLDOWN,
            )

    def is_unavailable(self) -> bool:
        return self._is_breaker_open()

    # -- 装配 + 探活 --------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def initialize(self) -> None:
        if not self._base_url:
            raise ValueError("AgentMemory HTTP base_url is required.")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "httpx is required for AgentMemory HTTP mode. Install with `pip install httpx`."
            ) from exc

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=self._read_timeout,
        )

        # 可选探活——绝不致命（服务端可能晚于宿主起来），只留日志线索（§6.2-G）。
        try:
            resp = await self._http.get("/healthz")
            if resp.status_code == 200:
                logger.info("[AgentMemoryMemoryProvider/http] connected to %s", self._base_url)
            else:
                logger.warning(
                    "[AgentMemoryMemoryProvider/http] healthz returned %s at %s",
                    resp.status_code,
                    self._base_url,
                )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider/http] healthz failed (%s): %s",
                self._base_url,
                exc,
            )
        self._is_initialized = True

    # -- scope 映射（§6.2-D：身份覆盖式） ------------------------------------ #

    def _scope_payload(self, scope) -> dict[str, Any]:
        """语义 _Scope → 服务端要求的嵌套 Scope 对象（身份覆盖式重映射）。

        服务端授权判定（sqlite/space_aware 权限管理器）：身份须覆盖 target——
        org/space 精确相等（硬边界）+ user 精确相等（最外层主体维）。故：
        - ``org``/``space`` ← 认证身份配置（dev 缺省 local/""）；
        - ``user`` ← 认证身份配置（dev 缺省 developer）；
        - ``agent`` ← 语义 scope 的 agent 维（rail 的 agent_id）；
        - ``session`` ← 语义 scope 的 user 维（rail 的 user_id）；
        - 语义 scope 的 org（scope_id）与 session（session_id）轴被丢弃——
          dev/单身份部署下无自由轴可承载（design doc R2 记录了该语义漂移）。

        身份覆盖式是唯一在 plain dev（allow_all）/ dev+identities / required
        三种部署形态下都成立的 payload 形态（design doc §6.2-D 部署形态表）。
        """
        return {
            "org": self._identity_org,
            "space": "",
            "user": self._identity_user,
            "agent": getattr(scope, "agent", ""),
            "session": getattr(scope, "user", ""),
        }

    # -- 统一分发 ------------------------------------------------------------- #

    async def _post_verb(
        self,
        verb: str,
        payload: dict,
        *,
        write: bool = False,
        raise_on_client_error: bool = False,
    ) -> Any:
        """POST ``/v1/<verb>``；返回解析后的 JSON body，传输失败返回 None。

        熔断开启或未初始化 → 直接 None。传输失败（网络/HTTP 错误/非 JSON）记入
        熔断器并降级返回 None，调用方按各自语义转成空结果——不向上抛，
        避免拖垮宿主的每轮 prefetch/sync_turn。

        4xx 是**业务拒绝**而非服务不可用：``raise_on_client_error=True`` 的调用方
        （显式工具）抛 :class:`AgentMemoryClientError` 把服务端错误信息透出给
        LLM（不计熔断）；其余调用方维持降级 None（既有行为不变）。
        """
        if self._http is None or not self._is_initialized or self._is_breaker_open():
            return None
        try:
            resp = await self._http.post(
                f"/v1/{verb}",
                json=payload,
                timeout=self._write_timeout if write else self._read_timeout,
            )
            if resp.is_error:
                if raise_on_client_error and 400 <= resp.status_code < 500:
                    raise AgentMemoryClientError(
                        f"/v1/{verb} rejected: HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                self._record_failure()
                logger.warning(
                    "[AgentMemoryMemoryProvider/http] /v1/%s -> HTTP %s: %s",
                    verb,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            self._record_success()
            return resp.json()
        except AgentMemoryClientError:
            raise
        except Exception as exc:
            self._record_failure()
            logger.warning(
                "[AgentMemoryMemoryProvider/http] /v1/%s failed: error_type=%s",
                verb,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _content_view(unit: dict) -> str:
        """从序列化 MemoryUnit 重建 content 视图。

        序列化不含顶层 ``content``（它是 property，不进 dataclasses.fields），
        按内核方式从 ``segments`` 换行拼接重建（对齐 mem2_provider）。
        """
        return "\n".join(
            seg.get("content", "") for seg in unit.get("segments", []) if isinstance(seg, dict)
        )

    # -- verb 实现 ------------------------------------------------------------ #

    async def add(
        self, content, scope, *, tags=None, system_metadata=None, user_metadata=None
    ) -> _AddResult | None:
        """写一条记忆。``system_metadata={"infer": "true"}`` 触发同步 LLM 抽取。

        响应是顶层数组（list[MemoryUnit] 序列化）。**空数组是合法结果**：
        infer=true 时派生记忆可能全量 dedup（update/noop）——返回
        ``_AddResult(item_id=None)`` 而非失败。传输失败才返回 None。
        """
        payload: dict[str, Any] = {
            "content": content,
            "scope": self._scope_payload(scope),
            "tags": tags or [],
        }
        if system_metadata:
            payload["system_metadata"] = system_metadata
        if user_metadata:
            payload["user_metadata"] = user_metadata
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/add tags=%s system_metadata=%s "
            "content_len=%d",
            metadata_for_log(tags or []),
            metadata_for_log(system_metadata or {}),
            len(content),
        )
        units = await self._post_verb("add", payload, write=True)
        if units is None:
            return None
        if not isinstance(units, list) or not units:
            return _AddResult(item_id=None)
        unit = units[0] if isinstance(units[0], dict) else {}
        return _AddResult(
            item_id=unit.get("id"),
            content=self._content_view(unit),
            tier=unit.get("tier", ""),
        )

    async def search(self, query, scope, *, top_k=10, filters=None) -> list[dict[str, Any]]:
        """混合检索。响应键是 ``items``（不是旧契约的 ``hits``）。"""
        payload: dict[str, Any] = {
            "query": query,
            "context": {"scope": self._scope_payload(scope)},
            "top_k": min(int(top_k or 10), _MAX_TOP_K),
            "disclosure": "l2",  # 全文，与 SDK 模式 DisclosureLevel.L2 对齐
        }
        if filters:
            payload["filters"] = filters
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/search query=%s top_k=%d filters=%s",
            redact_for_log(query),
            top_k,
            metadata_for_log(filters or {}),
        )
        data = await self._post_verb("search", payload)
        if not isinstance(data, dict):
            return []
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        return [
            {
                "item_id": it.get("unit_id", ""),
                "content": it.get("content", ""),
                "score": it.get("score", 0),
            }
            for it in items
            if isinstance(it, dict)
        ]

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        """列出 scope 下已建索引的记忆单元（POST /v1/list）。

        [本地修改 2026-10-17] profile 工具下线后暂无调用方，保留备用。
        """
        payload = {"scope": self._scope_payload(scope)}
        logger.info("[AgentMemoryMemoryProvider/http] POST /v1/list")
        data = await self._post_verb("list", payload)
        if not isinstance(data, dict):
            return []
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        return [
            {
                "item_id": it.get("id", ""),
                "content": self._content_view(it),
                "score": 0,
            }
            for it in items
            if isinstance(it, dict)
        ]

    def _context_payload(self, scope, project: str = "") -> dict[str, Any]:
        """语义 _Scope + project → ``Context`` dataclass 的 JSON 形态（F08）。

        api_contract ``_decode_dataclass`` 按字段镜像解码：``scope`` 嵌套对象 +
        ``extensions`` 可选。project 经 ``extensions["coords"]["project"]`` 下传
        （内核常量 ``COORDS_KEY = "coords"``，Engine 层 ``project_from_context``
        拆标量；缺失/空串 → 跨项目的 memory 根目录）。
        """
        context: dict[str, Any] = {"scope": self._scope_payload(scope)}
        if project:
            context["extensions"] = {"coords": {"project": project}}
        return context

    @classmethod
    def _unit_view(cls, unit: dict) -> dict[str, Any]:
        """序列化 MemoryUnit → provider 统一视图（content 从 segments 重建）。"""
        return {
            "item_id": unit.get("id", ""),
            "content": cls._content_view(unit),
            "tier": unit.get("tier", ""),
        }

    async def read_md_file(
        self, md_filename, scope, *, project="", line_range=None
    ) -> list[dict[str, Any]] | None:
        """读 md 文件（F08）。响应是 ``list[MemoryUnit]`` 序列化顶层数组（与 add 同构）。

        空数组是合法结果（文件不存在/行范围无完整块）；4xx（非文档模式等）抛
        :class:`AgentMemoryClientError` 透出；传输失败 None。
        """
        payload: dict[str, Any] = {
            "context": self._context_payload(scope, project),
            "md_filename": md_filename,
        }
        if line_range is not None:
            payload["line_range"] = [line_range[0], line_range[1]]
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/read_md_file file=%s project=%s "
            "line_range=%s",
            md_filename,
            metadata_for_log(project or ""),
            line_range,
        )
        units = await self._post_verb("read_md_file", payload, raise_on_client_error=True)
        if units is None:
            return None
        if not isinstance(units, list):
            return []
        return [self._unit_view(u) for u in units if isinstance(u, dict)]

    async def edit_md_file(
        self, md_filename, scope, old_text, new_text, *, project=""
    ) -> list[dict[str, Any]] | None:
        """md 文件内唯一替换（F08）。响应是受影响**新** unit 的顶层数组。

        4xx（old_text 不存在/不唯一、非文档模式）抛 :class:`AgentMemoryClientError`
        透出；传输失败 None。写路径超时（edit 同步 supersede shadow）。
        """
        payload: dict[str, Any] = {
            "context": self._context_payload(scope, project),
            "md_filename": md_filename,
            "old_text": old_text,
            "new_text": new_text,
        }
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/edit_md_file file=%s project=%s "
            "old_len=%d new_len=%d",
            md_filename,
            metadata_for_log(project or ""),
            len(old_text),
            len(new_text),
        )
        units = await self._post_verb(
            "edit_md_file", payload, write=True, raise_on_client_error=True
        )
        if units is None:
            return None
        if not isinstance(units, list):
            return []
        return [self._unit_view(u) for u in units if isinstance(u, dict)]

    async def evolve_extract(self, scope) -> None:
        """触发批量 EXTRACT。响应是裸 job_id 字符串（无信封）。"""
        payload = {"scope": self._scope_payload(scope), "mode": "extract"}
        logger.info("[AgentMemoryMemoryProvider/http] POST /v1/evolve mode=extract")
        job_id = await self._post_verb("evolve", payload, write=True)
        logger.info("[AgentMemoryMemoryProvider/http] /v1/evolve -> job_id=%s", job_id)

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception as exc:
                logger.debug("[AgentMemoryMemoryProvider/http] http client close failed: %s", exc)
        self._http = None
        self._is_initialized = False


# --------------------------------------------------------------------------- #
# 进程内客户端 —— 低延迟，search/list/evolve 是同步方法，须 to_thread 包
# --------------------------------------------------------------------------- #


class _InProcessClient(_AgentMemoryClient):
    """直接持有 ``LocalMemoryAPI`` 的进程内客户端。

    配置来源优先级（[本地修改 2026-10-17]）：``config_dict``（调用方在内存里
    缝合/内置的配置，如 provider 的 agent 默认装配）> ``config_path``（外部
    yml/json 文件）> 两者皆空 → ``assemble(None)`` 通用内核缺省（保留兼容，
    provider 正常路径不会再走到）。

    注意（§4.6）：``MemoryAPI`` 的 ``search``/``list``/``evolve`` 是同步方法
    （内部 ``asyncio.run`` 桥接），**不能在已有事件循环里直接调**（会抛
    ``RuntimeError: cannot be called from a running event loop``）。故用
    ``asyncio.to_thread`` 包同步方法到线程执行。唯一可直接 ``await`` 的是
    ``add_async``。

    与 HTTP 客户端共用 dict DSL 过滤器：``RetrievalQuery`` 边界统一 normalize
    旧 list、clause 与 dict DSL（``query_ops.py`` search 内注释）。
    """

    def __init__(
        self,
        config_path: str | None,
        *,
        config_dict: dict[str, Any] | None = None,
        security_provider: Any = None,
    ) -> None:
        if security_provider is None:
            # P1-2 fail-closed：进程内直连不再从业务 scope 自述身份。缺少可信身份
            # 来源时该模式不能作为生产路径——改走 HTTP/API Key，或由可信
            # composition root 注入 security_provider。
            raise RuntimeError(
                "AgentMemory in-process mode requires an injected trusted security "
                "provider (composition root); production should use HTTP + API key"
            )
        self._security_provider = security_provider
        config = config_dict
        if config is None and config_path:
            import json as _json
            from pathlib import Path

            p = Path(config_path)
            data = (
                _json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else _load_yaml(p)
            )
            config = data
        self._api = assemble(config=config)

    def _security(self) -> RequestSecurityContext:
        """本次调用的可信上下文：由注入的 provider 产出，业务 scope 只作 target。"""
        return self._security_provider()

    async def initialize(self) -> None:
        # 内核已在 __init__ 装配完成；无网络资源要建。
        return None

    @staticmethod
    def _to_api_scope(scope):
        """_Scope（轻量）→ api.Scope（进程内 AgentMemory API 期望的类型）。"""
        return ApiScope(
            org=getattr(scope, "org", ""),
            space=getattr(scope, "space", ""),
            user=getattr(scope, "user", ""),
            agent=getattr(scope, "agent", ""),
            session=getattr(scope, "session", ""),
        )

    async def add(
        self, content, scope, *, tags=None, system_metadata=None, user_metadata=None
    ) -> _AddResult | None:
        api_scope = self._to_api_scope(scope)
        try:
            units = await self._api.add_async(
                content,
                api_scope,
                source=Modality.TEXT,
                security=self._security(),
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider/in-process] add failed: error_type=%s",
                type(exc).__name__,
            )
            return None
        if not units:
            return _AddResult(item_id=None)
        unit = units[0]
        return _AddResult(
            item_id=unit.id,
            content=unit.content,
            tier=getattr(getattr(unit, "tier", None), "value", ""),
        )

    async def search(self, query, scope, *, top_k=10, filters=None) -> list[dict[str, Any]]:
        api_scope = self._to_api_scope(scope)
        try:
            result = await asyncio.to_thread(
                self._api.search,
                query,
                Context(scope=api_scope),
                security=self._security(),
                filters=filters,
                top_k=top_k,
                disclosure=DisclosureLevel.L2,
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider/in-process] search failed: error_type=%s",
                type(exc).__name__,
            )
            return []
        return [
            {"content": it.content, "score": it.score, "item_id": it.unit_id} for it in result.items
        ]

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        """列出 scope 下已建索引的记忆单元（MemoryAPI.list，同步方法须 to_thread）。

        [本地修改 2026-10-17] profile 工具下线后暂无调用方，保留备用。
        """
        api_scope = self._to_api_scope(scope)
        try:
            result = await asyncio.to_thread(
                self._api.list,
                api_scope,
                security=self._security(),
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider/in-process] list failed: error_type=%s",
                type(exc).__name__,
            )
            return []
        return [{"content": unit.content, "item_id": unit.id, "score": 0} for unit in result.items]

    @staticmethod
    def _md_context(api_scope, project: str = ""):
        """语义 scope + project → api.Context（F08）。

        project 经 ``extensions["coords"]["project"]`` 下传（``COORDS_KEY`` 常量
        与内核一致）；缺失/空串 → 跨项目的 memory 根目录。
        """
        from jiuwen_memory.common.type_def import COORDS_KEY

        extensions = {COORDS_KEY: {"project": project}} if project else {}
        return Context(scope=api_scope, extensions=extensions)

    async def read_md_file(
        self, md_filename, scope, *, project="", line_range=None
    ) -> list[dict[str, Any]] | None:
        api_scope = self._to_api_scope(scope)
        context = self._md_context(api_scope, project)
        # read_md_file 是同步方法（内部 asyncio.run），必须 to_thread（§4.6）。
        # 业务异常（非文档模式 ValidationError 等）不吞——显式工具调用须让
        # handle_tool_call 把错误透出给 LLM。
        units = await asyncio.to_thread(
            self._api.read_md_file,
            context,
            md_filename,
            line_range=line_range,
            security=self._security(),
        )
        return [
            {
                "item_id": unit.id,
                "content": unit.content,
                "tier": getattr(getattr(unit, "tier", None), "value", ""),
            }
            for unit in units
        ]

    async def edit_md_file(
        self, md_filename, scope, old_text, new_text, *, project=""
    ) -> list[dict[str, Any]] | None:
        api_scope = self._to_api_scope(scope)
        context = self._md_context(api_scope, project)
        # edit_md_file 同步方法（to_thread）；old_text 不存在/不唯一抛
        # NotFoundError/ValidationError，向上透出（见 read_md_file 注释）。
        units = await asyncio.to_thread(
            self._api.edit_md_file,
            context,
            md_filename,
            old_text,
            new_text,
            security=self._security(),
        )
        return [
            {
                "item_id": unit.id,
                "content": unit.content,
                "tier": getattr(getattr(unit, "tier", None), "value", ""),
            }
            for unit in units
        ]

    async def evolve_extract(self, scope) -> None:
        api_scope = self._to_api_scope(scope)

        # evolve 是同步+asyncio.run，必须 to_thread
        try:
            await asyncio.to_thread(
                self._api.evolve,
                api_scope,
                EvolveMode.EXTRACT,
                Channel.BACKGROUND,
                security=self._security(),
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider/in-process] evolve failed: error_type=%s",
                type(exc).__name__,
            )

    async def close(self) -> None:
        # LocalMemoryAPI 无显式 close；释放由 GC 处理
        pass


def _load_yaml(path):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML required to load YAML config; install with `uv sync --extra deploy`"
        ) from exc
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


__all__ = ["AgentMemoryMemoryProvider"]
