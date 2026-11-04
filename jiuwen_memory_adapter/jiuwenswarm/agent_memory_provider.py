# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMemory 外接记忆 provider —— 适配 JiuwenSwarm ExternalMemoryRail。

把 AgentMemory 的 ``MemoryAPI``（进程内 ``assemble_runtime()`` 或 HTTP ``/v1/<verb>``）
适配成 openjiuwen ``MemoryProvider`` ABC 的实现，供 ``ExternalMemoryRail`` /
``JiuwenMemoryRail`` 驱动。

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
- 双模式：``base_url`` 非空 → HTTP（全 async，推荐生产）；否则进程内 ``assemble_runtime()``。
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
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    # openjiuwen 是可选依赖（见 pyproject ``[project.optional-dependencies].agent``）：
    # 仅 jiuwenswarm 宿主驱动 ExternalMemoryRail 时需要作为 ABC 基类被继承。本仓库
    # 单元/集成测试不依赖宿主，未安装时降级为 object，保证 _prepare / _recall_filters
    # / _HttpClient / scope 映射等纯逻辑可被独立测试（生产环境装上后继承真正的 ABC）。
    from openjiuwen.core.memory.external.provider import MemoryProvider
except ImportError:
    MemoryProvider = object

from jiuwen_memory.api import (
    Channel,
    Context,
    DisclosureLevel,
    EvolveMode,
    Modality,
    RequestSecurityContext,
    assemble_runtime,
    install_privacy_filter,
    metadata_for_log,
    redact_for_log,
    scope_for_log,
)
from jiuwen_memory.api import Scope as ApiScope

# provider 通过 PYTHONPATH 以顶层模块 agent_memory_provider 被 import，
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
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to search across all projects. When "
                    "set, recalls this project's memories plus cross-project memories."
                ),
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
        # [本地修改] infer 恢复为 true：conclude 走 LLM 抽取并分类落盘，但语义仍是
        # 新增不改已有（更正仍路由到 edit_md_file）。
        "Store a new durable memory (extracted and classified). It NEVER modifies or "
        "removes existing memories — to change or correct what is already stored, use "
        "agent_memory_edit_md_file (read the file first to locate the exact text)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact/memory to store."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional labels for later filtering.",
            },
            "project": {
                "type": "string",
                "description": "Project coordinate; omit for cross-project storage.",
            },
        },
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

# 经验类别目录（mem2.0 §2.3 分类表，沿用 agent-core TTSE 的 categories.py，
# 首版固定不演进）：工具 schema 的 category 枚举与检索过滤共用一份。
_EXPERIENCE_CATEGORIES = (
    "external-service-automation",
    "documents-office-and-records",
    "data-analytics-and-visualization",
    "software-engineering-devops",
    "security-privacy-and-risk",
    "media-multimodal-and-creative",
    "science-engineering-modeling",
    "search-research-and-knowledge",
    "embodied-simulation-and-interactive-tasks",
    "skill-agent-meta-workflows",
    "other",
)

# 查经验工具的固定返回条数（mem2.0 §2.4：两类合起来 5 条，模型不可改）。
_EXPERIENCE_TOP_K = 5

# 周期整理注册间隔（秒，mem2.0 §2.5.1：一周一次的经验整理）。
_CONSOLIDATE_INTERVAL = 604_800


def _recall_filters() -> dict[str, Any]:
    """prefetch 召回过滤（dict DSL，HTTP/进程内两模式共用）。

    实测确认（design doc §8.2.1）：``{"tier": {"in": [...]}}`` 被服务端真实执行，
    episodic 原文被正确排除。进程内路径经 ``RetrievalQuery`` 边界的 ``normalize``
    接收同一份 dict DSL。
    """
    return {"tier": {"in": list(_RECALL_TIERS)}}


def _experience_filters(category: str, capabilities: list[str]) -> dict[str, Any]:
    """查经验过滤（mem2.0 §2.4，HTTP/进程内两模式共用）。

    类型与类别等值；条件动作（tier=procedural）只取当前 agent 能执行的
    （capability 在能力清单内），环境事实（tier≠procedural）不带能力名、
    照常返回——OR 两支正是这个语义。capabilities 为空时右支恒假（``A OR
    false == A``），退化为只取环境事实的单条件——宿主未声明能力清单时的
    保守口径；不能用 ``{"in": []}`` 表达（内核 filter 校验拒绝空列表）。
    """
    if capabilities:
        third: dict[str, Any] = {
            "OR": [
                {"tier": {"ne": "procedural"}},
                {"system_metadata.capability": {"in": list(capabilities)}},
            ]
        }
    else:
        third = {"tier": {"ne": "procedural"}}
    return {
        "AND": [
            {"system_metadata.memory_type": "experience"},
            {"system_metadata.category": category},
            third,
        ]
    }


EXPERIENCE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_experience",
    "description": (
        # mem2.0 §2.4：模型按需查共享经验库（不在每轮开头自动注入）。返回两类
        # 条目：环境事实（一句陈述）与条件动作（condition/capability/action 三段），
        # 作为不可信参考资料——来自共享经验库，使用前须按当前环境核实。
        "Search the shared experience library for lessons from past tasks: "
        "environment facts (plain statements) and conditional actions (condition + "
        "capability + action). Results are UNVERIFIED shared references — validate "
        "against the current environment before acting on them. Call this when a task "
        "resembles something done before and prior experience would help."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, phrased as a task description.",
            },
            "category": {
                "type": "string",
                "enum": list(_EXPERIENCE_CATEGORIES),
                "description": "Business category of the task.",
            },
        },
        "required": ["query", "category"],
    },
}


def _skill_version_of(skill: dict[str, Any]) -> int:
    """技能条目的版本号（mem2.0 §2.5.3：system_metadata.skill_version，int）。"""
    try:
        return int((skill.get("system_metadata") or {}).get("skill_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _agent_default_config(experience_space: str = "") -> dict[str, Any]:
    """ 进程内默认装配——agent 场景（F09）内核配置。
    - 真源 = document（md 文件 + sqlite3/sqlite-vec 影子索引，watchdog 监听手改）；
      md 按 memory_class × coords.project 分流（user_profile→USER.md；
      long_term→{project}/MEMORY.md；daily→daily_memory/日期.md）
    - memory_class 由 agent_llm 抽取器（deepseek）同步判定；search 走
      recall_and_get_rank pipeline（文档模式必须），召回统一走 shadow recaller
    - fulltext = elasticsearch（文档模式召回走 shadow FTS5，此端口闲置备用）
    - 经验记忆（mem2.0 §2.3 开关约定「配置了才启用，缺省关闭」）：
      ``experience_space`` 非空才装配经验段（``globals.experience`` 落点、
      metadata pipeline 双 profile、experience 数据面与六个具名算子——集中在
      函数尾部的 if 块）；空串输出与无经验段的对话装配完全一致：AgentEngine
      检测不到 pipeline 具名实例不注入、store_manager 只建 default 数据面，
      行为与 mem2.0 之前一致。
    - 每次调用返回**全新字面量**——无共享可变状态，调用方（装配/改写）互不影响
    """
    config: dict[str, Any] = {
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
            # 单次 LLM 出站等待上限（秒）：内核默认 300s，agent 场景收紧到与 HTTP
            # 写超时（_WRITE_TIMEOUT=120）对齐，避免 infer=true 抽取卡死时占满线程池。
            "llm_timeout": float(os.getenv("JIUWEN_MEMORY_TIMEOUT", "120")),
            # 只填到 /v1；SDK 会自动拼 /embeddings
            "embedder_base_url": os.getenv("EMBED_API_BASE", "https://api.siliconflow.cn/v1"),
            "embedder_api_key": os.getenv("EMBED_API_KEY", ""),
            "embedder_model": os.getenv("EMBED_MODEL", "BAAI/bge-m3"),
            "embedder_max_batch": int(os.getenv("EMBED_MAX_BATCH", "64")),
        },
        # ---- 存储 -------------------------------------------------------- #
        # document 模式下 KV 不再持有 MemoryUnit（真源在 md+shadow），但端口必须保留：
        # dreaming 注册表读写、lifecycle policy/sweep 数据源、装配期 manager.kv() 硬依赖。
        "kv_store": {"default": "memory"},
        # vector / fulltext / graph 三个 KV 时代索引端口已删除：文档模式召回统一走
        # shadow（FTS5 + vec0），这三路无消费者——回落内核 defaults 的内存版（同样闲置）。
        # md 真源 root
        "markdown_store": {"default": {"target": "local", "params": {
            "root": "",
        }}},
        # 影子索引（document 真源的机器索引：sqlite3 + sqlite-vec 复合）
        "shadow_index": {"default": {"target": "sqlite", "params": {"embedder": "default"}}},
        # 文档看门狗：md 保存后 debounce 毫秒再同步影子索引
        "watchdog": {"default": {"target": "watchdog", "params": {"debounce_ms": 2000}}},
        # 全局唯一 StoreManager（F07）：文档模式召回统一走 shadow_recaller。
        # experience 数据面（mem2.0）见函数尾部 if 块——经验/技能条目不走文档
        # 分流（write_document 显式 false 盖过 globals，ComponentConfig.get
        # params 优先），KV 真源 + keyword/vector 召回（fulltext/vector 端口走
        # 内核 defaults 内存版，与对话的 shadow 索引互不占用）。
        "store_manager": {"default": {"target": "composite", "params": {
            "domain_stores": {
                "default": {
                    "kv_store": "default",
                    "shadow_recaller": "shadow",
                    # 召回+物化在 storage 内一次完成（文档模式必须走此 pipeline）
                    "preferred_retrieval_pipeline": "recall_and_get_rank",
                },
            },
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
        "retriever": {
            "default": {"target": "pipeline", "params": {
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
            }},
        },
        # ---- 构建 ---------------------------------------------------------- #
        # agent 场景（F09）：prompt user↔agent 重定向 + cn 指令面，抽取时同步判定
        # memory_class 三类（md 落盘分流键），无独立 fallback 路径
        # 对话抽取（agent_llm）；经验抽取（experience，§2.2 三段）见尾部 if 块
        "extractor": {
            "default": {"target": "agent_llm",
                        "params": {"llm": "default", "extractor_language": "cn"}},
        },
        "abstractor": {"default": "concat"},
        "associator": {
            "default": {"target": "keyword", "params": {"feature_extractor": "default"}}
        },
        "classifier": {"default": "llm"},
        # 文档模式（write_document=true）：IndexBuilder 全委托 domain_store.add 文档分流；
        # 经验 IndexBuilder（mem2.0，见尾部 if 块）：hybrid 落 experience 数据面。
        "constructor": {
            "default": {"target": "document", "params": {"domain_store": "default"}},
        },
        # 文档模式去重（F08）：两路并跑（shadow FTS5 + vec）+ embedder cosine 精排；
        # 经验去重（mem2.0，见尾部 if 块）：vector dedup 查经验条目所在的 vector 端口。
        "dedup": {
            "default": {"target": "shadow", "params": {"embedder": "default"}},
        },
        "evolver": {
            "default": {"target": "orchestrating", "params": {
                "extractor": "default", "abstractor": "default", "associator": "default",
                "index_builder": "default", "message_store": "default",
                "dedup": "default", "llm": "default",
            }},
        },
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
    if not experience_space:
        return config
    # ---- 经验段（mem2.0 §2.3「配置了才启用，缺省关闭」）----------------------- #
    # globals 落点：经验固定 experience_space 空间（写入者空间之外的结构化沉淀），
    # 开启该空间的技能沉淀与周期整理。
    config["globals"]["experience"] = {
        "memory_class": "experience",
        "default_space": experience_space,
        "curated_spaces": [{"space": experience_space, "skill": True}],
    }
    # experience 数据面：经验/技能条目不走文档分流——write_document 显式 false
    # 盖过 globals（ComponentConfig.get params 优先），KV 真源 + keyword/vector
    # 召回（fulltext/vector 端口走内核 defaults 内存版，与对话的 shadow 索引
    # 互不占用）。
    config["store_manager"]["default"]["params"]["domain_stores"]["experience"] = {
        "write_document": False,
        "kv_store": "default",
        "keyword_recaller": "keyword",
        "vector_recaller": "vector",
        "preferred_retrieval_pipeline": "recall_and_get_rank",
    }
    # 经验检索（§2.4）：经验数据面 + 选取算子。parser/reranker 与对话检索共用
    # 具名实例（查询向量同源）；write_document 显式 false——本实例按非文档装配
    # （unit_reader 走 KV 点读、不补 DOCUMENT 通道）。
    config["retriever"]["experience"] = {"target": "pipeline", "params": {
        "reranker": "default",
        "query_parser": "default",
        "fuser": "default",
        "discloser": "default",
        "embedder": "default",
        "over_fetch_factor": 4,
        "over_fetch_floor": 60,
        "recall_max": 100,
        "rerank_max": 60,
        "min_score": 0.0,
        "min_score_ratio": 0.0,
        "min_score_ratio_uncalibrated": 0.0,
        "min_results": 0,
        "domain_store": "experience",
        "write_document": False,
        "selector_enabled": True,
    }}
    # 经验抽取（§2.2 三段：提炼/修正/分类）。
    config["extractor"]["experience"] = {"target": "experience", "params": {
        "llm": "default",
    }}
    # 经验 IndexBuilder：hybrid 落 experience 数据面（KV 正排 + fulltext/vector
    # 索引）。embedder 必须显式指名——dep 缺省匿名新建 hashing，与查询侧
    # parser 的 openai embedder 不同源会让向量召回失真。
    config["constructor"]["experience"] = {"target": "hybrid", "params": {
        "embedder": "default",
        "kv_store": "default",
        "fulltext_store": "default",
        "vector_store": "default",
    }}
    # 经验去重：vector dedup 查经验条目所在的 vector 端口，embedder 同上必须
    # 显式（匿名缺省 hashing 与索引向量不同源）。
    config["dedup"]["experience"] = {"target": "vector", "params": {
        "embedder": "default",
        "vector_store": "default",
        "kv_store": "default",
    }}
    # 经验 evolver（§2.2）：DynamicEvolver + 经验 extractor/appraiser（评价包住
    # 抽取）。index_builder/dedup 必须显式指名——dep 缺省按 globals.
    # write_document=true 解析成 document/shadow（文档算子），经验条目会被
    # 写进 md 文档。
    config["evolver"]["experience"] = {"target": "dynamic", "params": {
        "extractor": "experience",
        "appraiser": "experience",
        "index_builder": "experience",
        "dedup": "experience",
        "message_store": "default",
        "llm": "default",
    }}
    # 经验评价算子（§2.2 八步前五步）：author_key 是作者摘要的 HMAC salt（缺失
    # 装配失败）。生产经 JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY 注入；缺省固定值
    # 保证本地单机重启后摘要稳定（防同作者被判成不同人）。
    config["appraiser"] = {"experience": {"target": "experience", "params": {
        "llm": "default",
        "author_key": os.getenv(
            "JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY", "agent-memory-experience-hmac-key"
        ),
    }}}
    # metadata 双 profile 路由——AgentEngine 装配期检测到 pipeline 命名空间有
    # default 具名实例即注入（_opt_pipeline）。对话单元无 memory_type 路由值 →
    # fallback default（行为与无 pipeline 时一致：同一批具名算子）；
    # memory_type=experience → 经验算子组（§2.1 不回落语义）。
    config["pipeline"] = {"default": {"target": "metadata", "params": {
        "profiles": {
            "default": {
                "index_builder": "default",
                "retriever": "default",
                "evolver": "default",
            },
            "experience": {
                "index_builder": "experience",
                "retriever": "experience",
                "evolver": "experience",
            },
        },
        "fallback": "default",
    }}}
    return config


def _merge_config_over_default(
    config_dict: dict[str, Any], experience_space: str = ""
) -> dict[str, Any]:
    """[本地修改 2026-10-20] 把上层 ``config_dict`` 深合并到内置 agent 默认装配之上。

    进程内 ``config_dict`` 的语义从「整体替换」改为「以 ``_agent_default_config()``
    （F09 文档真源：md + shadow + agent engine + agent_llm 抽取）为基准深合并」：
    dict 对 dict 递归合并，标量/列表由上层值覆盖。这样宿主只传要覆盖的片段——
    凭证注入面（globals 的 ``llm_*`` / ``embedder_*`` 共 9 键）、
    ``globals.embedder_dim``、``markdown_store.default.params.root`` 等——未提及的
    agent 栈缺省（``write_document=true``、``engine=agent``、``shadow_index``、
    ``store_manager``、``recaller`` 等）原样保留，而不是掉回内核 defaults.py 的
    通用缺省（in_memory/echo/dynamic_llm/vector dedup 一路劣化）。

    合并发生在 provider 层而非内核层：若把部分 dict 直接投给 ``assemble_runtime``，
    内核 ``AssemblyContext.merged`` 的补全基准是 ``defaults.py``（不是 agent 默认），
    缺失的 F09 命名空间会被通用缺省补齐。返回值是 ``_agent_default_config()`` 的
    全新字面量被就地改写后的结果，默认配置本身每次调用都重建、无跨调用共享状态。

    [本地修改 2026-10-27] 经验段按 ``experience_space`` 条件装配（对齐方案 §2.3
    开关约定）：空串 → 合并基准不带经验段（上层显式配 ``globals.experience`` /
    ``pipeline`` 等仍经深合并尊重）；非空 → 基准带经验段且落点为该空间，上层
    显式配置同样覆盖。检索落点、周期整理与内核写入落点同空间，错位即查不到。
    """

    def _merge(base: Any, override: Any) -> Any:
        if isinstance(base, dict) and isinstance(override, dict):
            for key, value in override.items():
                base[key] = _merge(base[key], value) if key in base else value
            return base
        return override

    return _merge(_agent_default_config(experience_space=experience_space), config_dict)


class AgentMemoryMemoryProvider(MemoryProvider):
    """AgentMemory 适配 ``MemoryProvider`` ABC。

    构造参数（与 jiuwenswarm ``memory.external.agent_memory`` 子段对齐）：

    - ``base_url``：AgentMemory HTTP 服务地址（如 ``http://127.0.0.1:8137``）。
      非空 → HTTP 模式（推荐生产）；空 → 进程内模式。
    - ``config_path``：进程内模式的 AgentMemory 配置文件路径（YAML/JSON），
      **可选**——[本地修改 2026-10-17] 缺省时用内置 agent 场景默认装配
      ``_agent_default_config()``（F09 文档真源：md + shadow 索引 + agent_llm
      抽取，即 serve-doc 的代码化副本）；外部 yml 传入则覆盖内置默认。
    - ``config_dict``：进程内模式的两级命名空间配置 dict，**优先于
      ``config_path`` 与内置默认**。[本地修改 2026-10-20] 传给进程内分支时不再
      整体替换内置默认，而是经 ``_merge_config_over_default()`` 深合并到
      ``_agent_default_config()`` 之上——上层只需传要覆盖的片段（globals 凭证
      /端点、``globals.embedder_dim``、``markdown_store.default.params.root``
      等），未提及的 agent 栈缺省原样保留。
    - ``user_id``/``agent_id``：兼容常见记忆层隔离键（默认约定一致）。
    - ``security_provider``：进程内模式的可信身份来源——由可信 composition root
      注入的 ``() -> RequestSecurityContext``（P1-2 角色感知授权）。身份不由本
      provider 从 ``user_id``/``scope_id`` 自述（那些只是业务 target 维度，不是
      认证结论）；进程内直连缺少它时 initialize 会 fail-closed。
    - ``api_key``：HTTP 模式可选 Bearer 凭据（服务端 ``--auth-mode required`` 必需）。
    - ``identity_org``/``identity_user``：HTTP 模式服务端认证身份的 org/user
      （身份覆盖式 scope 的身份轴，§6.2-D）。缺省对齐 dev 认证器的固定身份
      ``local``/``developer``；生产配真实认证身份。
    - ``experience_space``：经验记忆的空间名（mem2.0 §2.4「查经验的空间」，与
      写入时经验落点一致）。非空 → 经验能力开启：注册查经验工具
      ``agent_memory_experience``、initialize 恢复 dreaming 并注册周期整理、
      进程内默认装配带上经验段（globals 落点 + 双 profile + 经验算子组）；
      **空串（缺省）→ 全部关闭**——对齐方案 §2.3 开关约定「配置了才启用，
      缺省关闭」：工具/周期任务/技能同步不启用，进程内默认装配不带经验段
      （与无经验段的对话装配完全一致，AgentEngine 不注入 pipeline），现有
      部署行为不受影响。进程内默认装配/config_dict 分支按本参数条件装配
      经验段；外部 config_path 全权，不覆盖。
    - ``experience_capabilities``：当前 agent 注册的工具与 skill 的名称清单
      （mem2.0 §2.4/§2.5.6 的能力清单，与写轨迹时的同一份，去掉查经验工具
      本身）。查经验时条件动作（tier=procedural）只返回清单内的；技能同步
      跳过用到清单外工具的。缺省空——只返回环境事实、不同步条件动作类技能
      （宿主未声明能力时的保守口径）。
    - ``skills_dir``：技能同步目录（mem2.0 §2.5.6）。非空 → initialize 同步
      一次、prefetch 距上次超过 ``skill_sync_interval`` 秒时后台同步，已发布
      技能（trial/approved）写到 ``skills_dir/_mem2/<skill_name>/SKILL.md``；
      空串 → 不同步。
    - ``skill_sync_interval``：技能同步间隔秒，缺省 3600（1 小时）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        config_path: str | None = None,
        config_dict: dict[str, Any] | None = None,
        user_id: str = "jiuwenswarm-user",
        agent_id: str = "jiuwenswarm",
        space: str = "",
        security_provider: Any = None,
        api_key: str = "",
        identity_org: str = "local",
        identity_user: str = "developer",
        experience_space: str = "",
        experience_capabilities: Sequence[str] | None = None,
        skills_dir: str = "",
        skill_sync_interval: float = 3600.0,
        **_kwargs: Any,
    ) -> None:
        self._base_url = base_url or ""
        self._config_path = config_path
        self._config_dict = config_dict
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
        # 经验记忆（mem2.0）：空间名非空即开启（缺省空串关闭，对齐方案 §2.3
        # 「配置了才启用，缺省关闭」）——查经验工具 / 周期任务 / 技能同步；
        # add_trajectory 是轨迹发送方的主动接口，不受此开关控制。
        self._experience_space = experience_space or ""
        self._experience_capabilities = list(experience_capabilities or ())
        self._skills_dir = skills_dir or ""
        self._skill_sync_interval = max(float(skill_sync_interval), 60.0)

        # Rail 在 before_invoke 首次调 initialize 时传入，覆盖构造默认
        self._scope_id: str = "__default__"
        self._session_id: str = "__default__"

        # 查经验工具的使用记录（mem2.0 §2.4 record_retrieved）：每次条目 id 与
        # 引用值，take_retrieved_refs 取走后由轨迹组装方并入 retrieved 字段
        # （§2.1）。asyncio 单线程内 append/take 无锁。
        self._retrieved_refs: list[dict[str, Any]] = []
        self._last_skill_sync: float = 0.0

        self._client: _AgentMemoryClient | None = None
        self._initialized = False

    @property
    def _experience_enabled(self) -> bool:
        return bool(self._experience_space)

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
                rest = query[len(prefix):].lstrip()
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
                config_dict=self._config_dict,
                api_key=self._api_key,
                identity_org=self._identity_org,
                identity_user=self._identity_user,
                experience_space=self._experience_space,
            )
        await self._client.initialize()
        self._initialized = True
        logger.info(
            "[AgentMemoryMemoryProvider] initialized (mode=%s, config=%s, scope=%s)",
            "http" if self._base_url else "in-process",
            self._base_url
            or (self._config_dict and "merged-dict")
            or self._config_path
            or "builtin-agent-default",
            scope_for_log(self.bound_scope()),
        )
        # mem2.0 §2.5.1：经验能力开启时恢复 dreaming 注册并在经验空间注册周期
        # 整理（一周一次 CONSOLIDATE，谓词限定 active 经验）。失败只记日志——
        # 多进程共用存储时只有一个能完成恢复，其余按设计继续。
        if self._experience_enabled and self._client is not None:
            await self._setup_experience_jobs()
        # mem2.0 §2.5.6：initialize 同步一次技能（skills_dir 未配置则跳过）。
        if self._skills_dir and self._experience_enabled:
            await self.sync_skills()

    async def _setup_experience_jobs(self) -> None:
        """恢复 dreaming 注册 + 注册经验空间的周期整理（mem2.0 §2.5.1）。"""
        client = self._client
        if client is None:
            # initialize 已按 _client 非空守卫；此处显式抛错防御误用（G.TES.01：
            # assert 在 -O 下会被剥离，不能承担运行期契约）。
            raise RuntimeError("experience jobs require an HTTP client")
        try:
            restored = await client.restore_dreaming()
            logger.info(
                "[AgentMemoryMemoryProvider] restore_dreaming restored=%s", restored
            )
        except Exception as exc:
            # 他人持锁（多进程共用内核）或恢复失败：记日志继续，不阻塞记忆主链路。
            logger.warning(
                "[AgentMemoryMemoryProvider] restore_dreaming failed: error_type=%s",
                type(exc).__name__,
            )
        try:
            await client.register_consolidation(
                self._experience_scope(), _CONSOLIDATE_INTERVAL
            )
            logger.info(
                "[AgentMemoryMemoryProvider] consolidation registered space=%s "
                "interval=%ss",
                self._experience_space,
                _CONSOLIDATE_INTERVAL,
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] consolidation registration failed: "
                "error_type=%s",
                type(exc).__name__,
            )

    def _experience_scope(self) -> _Scope:
        """查经验/技能/周期整理的空间 scope（mem2.0 §2.4：不带 user——经验
        条目不属于任何用户；space 维为构造参数 experience_space）。"""
        return _Scope(org=self._scope_id, space=self._experience_space)

    # -- MemoryProvider: 工具声明 + 调度 -------------------------------------- #

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI 风格 schema；Rail 自动包成 ToolCard（§4.8）。

        [本地修改 2026-10-17] ``agent_memory_profile`` / ``agent_memory_procedural``
        **不在注册列表**——LLM 看不到、不会主动调用；但两者的 schema 与
        ``handle_tool_call`` 实现完整保留，供内部直调（测试 / 排查 / 后续
        重新注册时直接加回列表即可）。
        """
        schemas = [SEARCH_SCHEMA, CONCLUDE_SCHEMA, READ_MD_SCHEMA, EDIT_MD_SCHEMA]
        # mem2.0 §2.4：经验能力开启时注册查经验工具（模型按需调用）。
        if self._experience_enabled:
            schemas.append(EXPERIENCE_SCHEMA)
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
                # [本地修改 2026-10-19] 对齐 mem2_search：project 走 filters 下推为
                # system_metadata.project 谓词，与写入侧 add（project 作标量
                # system_metadata["project"]）同口径；IN ['', project] 同时召回跨项目
                # 可见记忆与同 project 记忆。
                project = str(args.get("project") or "").strip()
                filters = (
                    {"system_metadata.project": {"in": ["", project]}} if project else None
                )
                # 显式检索不过滤 tier（§6.2-F）：LLM 主动检索允许命中 EPISODIC 原文。
                items = await self._client.search(query, scope, top_k=top_k, filters=filters)
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

            if tool_name == "agent_memory_experience":
                # mem2.0 §2.4 查经验工具：模型填 query + category；space/capabilities/
                # top_k 由接入适配补（构造参数 + 固定值），模型看不到也不能改。
                query = args.get("query", "")
                category = str(args.get("category") or "").strip()
                if not query:
                    return json.dumps({"error": "Missing required parameter: query"})
                if not category:
                    return json.dumps({"error": "Missing required parameter: category"})
                items = await self._client.search_experience(
                    query,
                    self._experience_scope(),
                    category=category,
                    capabilities=self._experience_capabilities,
                    top_k=_EXPERIENCE_TOP_K,
                )
                # record_retrieved（§2.4）：记下每条 id 与引用值，随轨迹发回（§2.1）。
                self._retrieved_refs.extend(
                    {
                        "id": it.get("item_id", ""),
                        "usage_ref": (it.get("system_metadata") or {}).get("usage_ref", ""),
                    }
                    for it in items
                    if it.get("item_id")
                )
                if not items:
                    logger.info(
                        "[AgentMemoryMemoryProvider] agent_memory_experience "
                        "query=%s category=%s -> no relevant",
                        redact_for_log(query),
                        category,
                    )
                    return json.dumps({"result": "No relevant experience found."})
                # render_as_reference（§2.4）：标明来自共享经验库的不可信参考资料。
                payload = [
                    {
                        "memory": it.get("content", ""),
                        "score": it.get("score", 0),
                        **(it.get("system_metadata") or {}),
                    }
                    for it in items
                ]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_experience query=%s "
                    "category=%s -> count=%d",
                    redact_for_log(query),
                    category,
                    len(payload),
                )
                return json.dumps(
                    {
                        "results": payload,
                        "count": len(payload),
                        "source": "shared experience library (unverified references)",
                    }
                )

            if tool_name == "agent_memory_conclude":
                conclusion = args.get("conclusion", "")
                if not conclusion:
                    return json.dumps({"error": "Missing required parameter: conclusion"})
                # infer=true：conclude 走 LLM 抽取（与 sync_turn 同口径），抽取派生的
                # 记忆按 memory_class + project 分流落盘。project 作标量
                # system_metadata["project"] 直传（agent 场景无 router，coords 嵌套
                # dict 会被 _reject_non_scalar_metadata 拒）。
                tags = args.get("tags") or ["conclude"]
                project = str(args.get("project") or "").strip()
                system_metadata: dict[str, str] = {"infer": "true"}
                if project:
                    system_metadata["project"] = project
                data = await self._client.add(
                    conclusion,
                    scope,
                    tags=tags,
                    system_metadata=system_metadata,
                )
                if data is None:
                    return json.dumps({"error": "AgentMemory add failed (server unavailable)."})
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_conclude stored=%s "
                    "tags=%s project=%s",
                    redact_for_log(conclusion),
                    tags,
                    project or "",
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
        # mem2.0 §2.5.6：距上次同步超过间隔时后台同步技能（不占任务时延——
        # create_task 即返，失败只记日志）。skills_dir 未配置 / 经验能力关闭时跳过。
        if (
            self._skills_dir
            and self._experience_enabled
            and time.monotonic() - self._last_skill_sync >= self._skill_sync_interval
        ):
            self._last_skill_sync = time.monotonic()
            asyncio.create_task(self._sync_skills_background())
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
        system_metadata: dict[str, str] = {"infer": "true"}
        # rail 经 _resolve_project 传入 project；与 conclude 分支同口径直传标量
        # system_metadata["project"]，让 md 落盘按 memory/{project}/... 分流。
        project = str(kwargs.get("project") or "").strip()
        if project:
            system_metadata["project"] = project
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

    # -- 轨迹转交（mem2.0 §2.1：Mem2TrajectoryStore 的发送出口） ----------------- #

    async def add_trajectory(
        self,
        content: str,
        system_metadata: dict[str, str] | None,
        session_id: str,
    ) -> None:
        """轨迹转交（mem2.0 §2.1）：``memory_type=experience`` + ``infer=true``
        经 ``add`` 交给经验 profile。

        与对话写入（sync_turn）的三点差异，都由轨迹链路的重发要求决定：

        - **归属主体**：取 provider 的租户/用户/agent 与本参数的 ``session_id``
          （轨迹载体按会话隔离，不混入 provider 当前的会话维）。
        - **出错抛回**：sync_turn 的 add 失败只记日志（丢一轮对话可接受），本方法
          把错误抛回给调用方（Mem2TrajectoryStore 按可重试错误退避重发）——不吞。
        - ``system_metadata`` 由调用方组好概要字段（trajectory_id /
          previous_trajectory_id / tool_call_count / retrieved_count / coords /
          external_score），本方法只并入 memory_type 与 infer 两个路由键。

        """
        if self._client is None:
            raise RuntimeError("provider not initialized")
        metadata: dict[str, str] = {"memory_type": "experience", "infer": "true"}
        if system_metadata:
            metadata.update(
                {str(k): str(v) for k, v in system_metadata.items() if v is not None}
            )
        scope = _Scope(
            org=self._scope_id,
            space=self._space,
            user=self._user_id,
            agent=self._agent_id,
            session=session_id,
        )
        logger.info(
            "[AgentMemoryMemoryProvider] add_trajectory session=%s "
            "system_metadata=%s content_len=%d",
            metadata_for_log({"session_id": session_id}),
            metadata_for_log(metadata),
            len(content),
        )
        # client.add_trajectory 不降级——异常原样抛回。
        await self._client.add_trajectory(content, scope, system_metadata=metadata)

    def take_retrieved_refs(self) -> list[dict[str, str]]:
        """取走查经验工具累计的使用记录（§2.4 record_retrieved 的读出口）。

        每项 ``{"id", "usage_ref"}``；取走即清空。轨迹组装方在组 retrieved 字段
        （§2.1）时调用；不组轨迹的部署不调用、记录滞留至下次取走（量级为每
        次工具调用至多 top_k 条，不构成泄漏）。
        """
        refs, self._retrieved_refs = self._retrieved_refs, []
        return [
            {
                "id": str(ref.get("id", "")),
                "usage_ref": str(ref.get("usage_ref", "")),
            }
            for ref in refs
        ]

    # -- 技能同步（mem2.0 §2.5.6） --------------------------------------------- #

    # 技能名只接受小写字母/数字/连字符，不以连字符开头结尾，长度 1-64（模型生成、
    # 编辑者可改，同步时不信任——§2.5.6 技能名校验）。
    _SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")

    async def _sync_skills_background(self) -> None:
        """prefetch 触发的后台同步入口：吞异常（不影响本轮检索）。"""
        try:
            await self.sync_skills()
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] background skill sync failed: "
                "error_type=%s",
                type(exc).__name__,
            )

    async def sync_skills(self) -> None:
        """把已发布技能同步到 ``skills_dir/_mem2/<skill_name>/SKILL.md``（§2.5.6）。

        流程：取（list 已发布技能——memory_type=skill、标签 trial/approved、
        生效）→ 筛选（同名取版本号最大；跳过技能名不合规 / 用到清单外工具 /
        本地已有同名技能）→ 写（内容无变化不重写）→ 清理（_mem2 下不在本次
        结果里的目录删除，即被驳回/取代/下线的技能）。取不到时不动现有文件。
        """
        if not self._skills_dir or not self._experience_enabled:
            return
        if self._client is None:
            raise RuntimeError("provider not initialized")
        # 1 取技能：失败即返回（不动现有文件，下次再试）。
        skills = await self._client.list_published_skills(self._experience_scope())
        self._last_skill_sync = time.monotonic()
        # 2 筛选：同名取 skill_version 最大的一版。
        by_name: dict[str, dict[str, Any]] = {}
        for skill in skills:
            name = str(skill.get("skill_name", "")).strip()
            if not name:
                continue
            current = by_name.get(name)
            if current is None or _skill_version_of(skill) > _skill_version_of(current):
                by_name[name] = skill
        mem2_dir = Path(self._skills_dir) / "_mem2"
        selected: dict[str, dict[str, Any]] = {}
        for name, skill in by_name.items():
            # 技能名不合规：跳过（不信任模型/编辑者给的目录名）。
            if not self._SKILL_NAME_RE.match(name):
                logger.warning(
                    "[AgentMemoryMemoryProvider] skill sync skipped invalid "
                    "skill_name=%s",
                    redact_for_log(name),
                )
                continue
            # 本地已有同名技能（_mem2 之外的技能目录）：跳过，本地优先。
            if (Path(self._skills_dir) / name).exists():
                logger.info(
                    "[AgentMemoryMemoryProvider] skill sync skipped local-existing "
                    "skill_name=%s",
                    name,
                )
                continue
            # 能力清单非空时，技能用到的工具须全部在清单内（§2.5.6 跳过执行
            # 不了的）；清单为空视同取不到，本次不按能力筛选（与 §2.5.6 的
            # 「启动时还取不到这份清单的，本次不按能力筛选」同义）。
            required = skill.get("capabilities") or []
            if self._experience_capabilities and not set(
                str(item) for item in required
            ).issubset(self._experience_capabilities):
                logger.info(
                    "[AgentMemoryMemoryProvider] skill sync skipped beyond-"
                    "capability skill_name=%s",
                    name,
                )
                continue
            selected[name] = skill
        # 3 写文件：条目正文即 SKILL.md 全文；内容无变化不重写（保留 mtime，
        # SkillUseRail 按修改时间发现变化）。路径解析校验：目标必须在 _mem2 内。
        mem2_dir.mkdir(parents=True, exist_ok=True)
        mem2_resolved = mem2_dir.resolve()
        for name, skill in selected.items():
            target = (mem2_dir / name / "SKILL.md").resolve()
            if not str(target).startswith(str(mem2_resolved)):
                logger.warning(
                    "[AgentMemoryMemoryProvider] skill sync path escaped _mem2, "
                    "skipped skill_name=%s",
                    name,
                )
                continue
            content = str(skill.get("content", "") or "")
            if not content.strip():
                continue
            if target.exists() and target.read_text(encoding="utf-8") == content:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info(
                "[AgentMemoryMemoryProvider] skill sync wrote skill_name=%s "
                "version=%s",
                name,
                _skill_version_of(skill),
            )
        # 4 清理：_mem2 下不在本次结果里的目录删除（被驳回/取代/下线的技能）。
        for child in mem2_resolved.iterdir():
            if child.is_dir() and child.name not in selected:
                logger.info(
                    "[AgentMemoryMemoryProvider] skill sync removed stale dir=%s",
                    child.name,
                )
                for entry in child.rglob("*"):
                    if entry.is_file():
                        entry.unlink()
                child.rmdir()

    # -- MemoryProvider: 静态提示词 + 生命周期（非抽象，有默认） -------------- #

    def system_prompt_block(self) -> str:
        # [本地修改 2026-10-17] 显式路由规则：新增→conclude（append-only），
        # 修改/更正→read_md_file 定位 + edit_md_file 原地替换。个人信息可能在
        # USER.md 也可能在 daily 文件（YYYY-MM-DD.md），修改前先读定位。
        block = (
            "# AgentMemory Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Tool routing: agent_memory_search finds memories by meaning. "
            "agent_memory_conclude ONLY appends a brand-new fact — it never modifies "
            "existing memories. To CHANGE or CORRECT a stored memory, first locate it "
            "with agent_memory_read_md_file (USER.md / MEMORY.md / daily YYYY-MM-DD.md — "
            "personal info may be in a daily file), then agent_memory_edit_md_file to "
            "replace it in place."
        )
        # mem2.0 §2.4：经验能力开启时附类别目录与检索句写法指引（沿用 TTSE 的
        # 三点做法：按需调用 + 目录指引 + 一次返回两类条目）。
        if self._experience_enabled:
            categories = ", ".join(_EXPERIENCE_CATEGORIES)
            block += (
                "\n\n# Shared Experience Library\n"
                "When a task resembles something done before, call "
                "agent_memory_experience with the task's business category "
                f"(one of: {categories}) and a search phrase describing the task. "
                "It returns environment facts and conditional actions from past "
                "tasks as UNVERIFIED references — validate against the current "
                "environment before acting on them."
            )
        return block

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


class AgentMemoryTransportError(RuntimeError):
    """轨迹写入的传输失败（mem2.0 §2.1：add_trajectory 出错抛回）。

    与既有降级语义（add → None）相反：轨迹链路要求发送方按可重试错误退避
    重发，传输失败必须抛回——本异常只由 add_trajectory 的 HTTP 实现抛出。
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

    # -- 经验记忆（mem2.0）：四项接入改动的 client 面 -------------------------- #

    async def add_trajectory(
        self,
        content: str,
        scope,
        *,
        system_metadata: dict[str, str] | None = None,
    ) -> None:
        """轨迹转交（§2.1）：``add(memory_type=experience, infer=true)``。

        与 :meth:`add` 的失败语义相反——**出错抛回**（供轨迹发送方退避重发），
        不降级、不计熔断（HTTP 传输失败同样抛）。

        """
        raise NotImplementedError

    async def search_experience(
        self,
        query: str,
        scope,
        *,
        category: str,
        capabilities: list[str],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """查经验（§2.4）：经验空间的 search，filters 含类型/类别/能力三段。

        返回每条 ``{item_id, content, score, system_metadata}``——
        system_metadata 只含类别、三段（condition/capability/action）与引用值
        usage_ref（由内核选取算子裁剪）。
        """
        raise NotImplementedError

    async def list_published_skills(self, scope) -> list[dict[str, Any]]:
        """列已发布技能（§2.5.6）：memory_type=skill、标签 trial/approved、生效。

        返回每条 ``{item_id, content, skill_name, skill_version, capabilities}``。
        """
        raise NotImplementedError

    async def restore_dreaming(self) -> bool:
        """恢复 dreaming 注册（§2.5.1）。返回是否执行了恢复（HTTP surface 未
        暴露该 verb 时返回 False——由服务端侧自行恢复）。
        """
        return False

    async def register_consolidation(self, scope, interval: int) -> None:
        """在 scope 空间注册周期整理（§2.5.1：CONSOLIDATE + dreaming=True）。"""
        raise NotImplementedError

    async def close(self) -> None:
        pass

    @staticmethod
    def is_unavailable() -> bool:
        return False


def _build_client(
    base_url: str,
    config_path: str | None,
    *,
    security_provider: Any = None,
    config_dict: dict[str, Any] | None = None,
    api_key: str = "",
    identity_org: str = "local",
    identity_user: str = "developer",
    experience_space: str = "",
) -> _AgentMemoryClient:
    """按优先级构建客户端：HTTP > 直传 config_dict > 外部配置文件 > 内置 agent 默认装配。

    [本地修改 2026-10-17] 进程内分支缺省不再回落 ``assemble_runtime(None)``（echo LLM +
    内存 KV 的通用内核缺省），而是用 ``_agent_default_config()``（agent 场景 F09
    文档真源装配，serve-doc 的代码化副本）。
    [本地修改 2026-10-20] ``config_dict`` 分支经 ``_merge_config_over_default()``
    深合并到内置默认之上（而非整体替换）：上层传部分片段时保留 agent 栈缺省。
    [本地修改 2026-10-27] 经验记忆（mem2.0 §2.3 开关约定「配置了才启用，缺省
    关闭」）：``experience_space`` 空串 → 进程内装配不带经验段（config_dict 分支
    的合并基准同样不带；上层显式配 ``globals.experience`` 等仍经深合并尊重）；
    非空 → 装配经验段且落点为该空间，与 provider 的检索/工具/周期任务一致。
    外部 ``config_path`` 全权，不注入（部署方自配）。

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

    if config_dict:
        # [本地修改 2026-10-20] 上层 config_dict 深合并到内置 agent 默认装配之上，
        # 而非整体替换——宿主只传要覆盖的片段（globals 凭证/端点、embedder_dim、
        # markdown_store.default.params.root），未提及的 F09 栈缺省原样保留。
        # 经验段按 experience_space 条件进合并基准（2026-10-27，见上）。
        return _InProcessClient(
            None,
            config_dict=_merge_config_over_default(config_dict, experience_space),
            security_provider=security_provider,
        )
    if config_path:
        return _InProcessClient(config_path, security_provider=security_provider)
    return _InProcessClient(
        None,
        config_dict=_agent_default_config(experience_space=experience_space),
        security_provider=security_provider,
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

        [本地修改 2026-10-26] space 维透传语义 scope（此前硬编码空串）——经验
        记忆（mem2.0）的检索/技能列举/周期整理都发生在显式空间（构造参数
        experience_space），对话路径 space 仍为空串，行为不变。
        """
        return {
            "org": self._identity_org,
            "space": getattr(scope, "space", "") or "",
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

    # -- 经验记忆（mem2.0） ---------------------------------------------------- #

    async def add_trajectory(
        self, content, scope, *, system_metadata=None
    ) -> None:
        """轨迹转交（§2.1）。出错抛回——不走 ``_post_verb`` 的降级语义（None），
        传输失败/HTTP 错误均抛 ``AgentMemoryTransportError`` 供发送方重发。
        """
        payload: dict[str, Any] = {
            "content": content,
            "scope": self._scope_payload(scope),
            "tags": [],
        }
        if system_metadata:
            payload["system_metadata"] = system_metadata
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/add trajectory "
            "system_metadata=%s content_len=%d",
            metadata_for_log(system_metadata or {}),
            len(content),
        )
        if self._http is None or not self._is_initialized or self._is_breaker_open():
            raise AgentMemoryTransportError(
                "AgentMemory HTTP client unavailable (not initialized or breaker open)"
            )
        try:
            resp = await self._http.post(
                "/v1/add", json=payload, timeout=self._write_timeout
            )
        except Exception as exc:
            self._record_failure()
            raise AgentMemoryTransportError(
                f"trajectory add transport failed: {type(exc).__name__}"
            ) from exc
        if resp.is_error:
            self._record_failure()
            raise AgentMemoryTransportError(
                f"/v1/add trajectory rejected: HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        self._record_success()

    async def search_experience(
        self, query, scope, *, category, capabilities, top_k=5
    ) -> list[dict[str, Any]]:
        """查经验（§2.4）：filters 三段 + extensions 路由到经验 profile。

        响应 items 的 system_metadata 已由内核选取算子裁剪（类别/三段/usage_ref），
        原样透传给上层渲染。
        """
        payload: dict[str, Any] = {
            "query": query,
            "context": {
                "scope": self._scope_payload(scope),
                # 路由键（§2.3）：extensions 优先于 filters 选 profile
                "extensions": {"memory_type": "experience"},
            },
            "filters": _experience_filters(category, capabilities),
            "top_k": min(int(top_k or 5), _MAX_TOP_K),
            "disclosure": "l2",
        }
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/search experience "
            "category=%s top_k=%d",
            metadata_for_log({"category": category}),
            top_k,
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
                "system_metadata": it.get("system_metadata") or {},
            }
            for it in items
            if isinstance(it, dict)
        ]

    async def list_published_skills(self, scope) -> list[dict[str, Any]]:
        """列已发布技能（§2.5.6）：类型 + 标签 + 生效三重过滤。"""
        payload = {
            "scope": self._scope_payload(scope),
            "memory_types": ["skill"],
            "filters": {
                "AND": [
                    {"tags": {"in": ["approved", "trial"]}},
                    {"lifecycle": "active"},
                ]
            },
            "limit": 1000,
        }
        logger.info("[AgentMemoryMemoryProvider/http] POST /v1/list skills")
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
                "system_metadata": it.get("system_metadata") or {},
                "skill_name": (it.get("system_metadata") or {}).get("skill_name", ""),
                "skill_version": (it.get("system_metadata") or {}).get(
                    "skill_version", 0
                ),
                "capabilities": (it.get("system_metadata") or {}).get(
                    "capabilities", []
                ),
            }
            for it in items
            if isinstance(it, dict)
        ]

    async def restore_dreaming(self) -> bool:
        """HTTP surface 未暴露 restore_dreaming verb（非 MemoryAPI 公共契约）——
        由服务端侧自行恢复，本方法如实返回 False。
        """
        logger.info(
            "[AgentMemoryMemoryProvider/http] restore_dreaming not exposed over "
            "HTTP; server-side responsibility"
        )
        return False

    async def register_consolidation(self, scope, interval: int) -> None:
        """注册周期整理（§2.5.1）：CONSOLIDATE + dreaming=True + interval。"""
        payload: dict[str, Any] = {
            "scope": self._scope_payload(scope),
            "mode": "consolidate",
            "dreaming": True,
            "interval": int(interval),
            "candidate": {
                "type": "predicate",
                "filters": {
                    "lifecycle": "active",
                    "system_metadata.memory_type": "experience",
                    "tier": {"in": ["semantic", "procedural"]},
                },
            },
        }
        logger.info(
            "[AgentMemoryMemoryProvider/http] POST /v1/evolve consolidate "
            "dreaming=True interval=%s",
            interval,
        )
        await self._post_verb("evolve", payload, write=True)

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
    yml/json 文件）> 两者皆空 → ``assemble_runtime(None)`` 通用内核缺省（保留兼容，
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

            p = Path(config_path)
            data = (
                _json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else _load_yaml(p)
            )
            config = data
        # assemble_runtime 而非 assemble：保留 start/close 生命周期句柄——文档模式
        # （watch_document=true）下 start_background 起看门狗（监听 md 手改同步影子
        # 索引），close 释放 shadow sqlite。
        self._runtime = assemble_runtime(config=config)
        self._api = self._runtime.api

    def _security(self) -> RequestSecurityContext:
        """本次调用的可信上下文：由注入的 provider 产出，业务 scope 只作 target。"""
        return self._security_provider()

    async def initialize(self) -> None:
        # 内核已在 __init__ 装配完成；无网络资源要建。文档模式（watch_document=true）
        # 下起看门狗（监听 md 手改同步影子索引），须在 initialize 的 async 上下文调
        # （看门狗内部 asyncio.get_running_loop 绑 agent 主循环）；无文档模式时 no-op。
        try:
            await self._runtime.start_background()
        except Exception as exc:
            # 看门狗起不来不阻塞记忆主链路（add/search 不依赖监听），仅监听不可用。
            logger.warning(
                "[AgentMemoryMemoryProvider/in-process] start_background failed "
                "(document watch unavailable): %s",
                exc,
            )

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
        # COORDS_KEY = "coords"（jiuwen_memory.common.type_def）——access 层
        # （boundary 契约 A-01）只允许 import jiuwen_memory.api，不能直接 import
        # 内核 common.type_def。该常量是稳定字面量 "coords"，与内核一致地写死，
        # 由 Context.extensions 透传给 Engine 层 _project_from_context 解析。
        extensions = {"coords": {"project": project}} if project else {}
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

    # -- 经验记忆（mem2.0） ---------------------------------------------------- #

    async def add_trajectory(
        self, content, scope, *, system_metadata=None
    ) -> None:
        """轨迹转交（§2.1）：add_async 原生协程，异常原样抛回（不降级）。"""
        api_scope = self._to_api_scope(scope)
        await self._api.add_async(
            content,
            api_scope,
            source=Modality.TEXT,
            security=self._security(),
            tags=[],
            system_metadata=system_metadata,
        )

    async def search_experience(
        self, query, scope, *, category, capabilities, top_k=5
    ) -> list[dict[str, Any]]:
        """查经验（§2.4）：extensions 路由 + filters 三段，同步方法须 to_thread。"""
        api_scope = self._to_api_scope(scope)
        result = await asyncio.to_thread(
            self._api.search,
            query,
            Context(
                scope=api_scope,
                extensions={"memory_type": "experience"},
            ),
            security=self._security(),
            filters=_experience_filters(category, capabilities),
            top_k=top_k,
            disclosure=DisclosureLevel.L2,
        )
        return [
            {
                "item_id": it.unit_id,
                "content": it.content,
                "score": it.score,
                # system_metadata 由内核选取算子裁剪（类别/三段/usage_ref）
                "system_metadata": dict(getattr(it, "system_metadata", None) or {}),
            }
            for it in result.items
        ]

    async def list_published_skills(self, scope) -> list[dict[str, Any]]:
        """列已发布技能（§2.5.6）：类型 + 标签 + 生效三重过滤（list 须 to_thread）。"""
        api_scope = self._to_api_scope(scope)
        result = await asyncio.to_thread(
            self._api.list,
            api_scope,
            security=self._security(),
            memory_types=["skill"],
            filters={
                "AND": [
                    {"tags": {"in": ["approved", "trial"]}},
                    {"lifecycle": "active"},
                ]
            },
            limit=1000,
        )
        skills: list[dict[str, Any]] = []
        for unit in result.items:
            meta = dict(getattr(unit, "system_metadata", None) or {})
            skills.append(
                {
                    "item_id": unit.id,
                    "content": unit.content,
                    "system_metadata": meta,
                    "skill_name": meta.get("skill_name", ""),
                    "skill_version": meta.get("skill_version", 0),
                    "capabilities": meta.get("capabilities", []),
                }
            )
        return skills

    async def restore_dreaming(self) -> bool:
        """恢复 dreaming 注册（§2.5.1）。非 MemoryAPI 公共契约——按属性探测，
        会持实例锁（他人持有抛 RuntimeError，由上层记日志继续）。
        """
        restore = getattr(self._api, "restore_dreaming", None)
        if restore is None:
            logger.info(
                "[AgentMemoryMemoryProvider/in-process] api has no "
                "restore_dreaming attribute"
            )
            return False
        await asyncio.to_thread(restore)
        return True

    async def register_consolidation(self, scope, interval: int) -> None:
        """注册周期整理（§2.5.1）：CONSOLIDATE + dreaming=True + interval 秒。"""
        api_scope = self._to_api_scope(scope)
        await asyncio.to_thread(
            self._api.evolve,
            api_scope,
            EvolveMode.CONSOLIDATE,
            Channel.BACKGROUND,
            security=self._security(),
            candidate={
                "type": "predicate",
                "filters": {
                    "lifecycle": "active",
                    "system_metadata.memory_type": "experience",
                    "tier": {"in": ["semantic", "procedural"]},
                },
            },
            dreaming=True,
            interval=int(interval),
        )

    async def close(self) -> None:
        # 停看门狗/释放 shadow sqlite（文档模式），幂等；runtime.close 是同步方法。
        if self._runtime is not None:
            try:
                self._runtime.close()
            except Exception as exc:
                logger.warning(
                    "[AgentMemoryMemoryProvider/in-process] runtime close failed: %s",
                    exc,
                )
            self._runtime = None


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
