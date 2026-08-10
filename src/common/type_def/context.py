"""Context — 调用上下文：目标范围 + 调用级透传配置。

把「在谁的范围内」（``scope``）与「调用级透传配置」（``extensions``）打包成调用端
便利结构。**Context 只活在接口层**：API 方法在边界处把它拆开——target ``scope`` 照旧
作为独立轴下推（鉴权/检索），``extensions`` 经 API 边界写入调用级 options、顺 parser
进 ``ParsedQuery``，透传给（用户自定义的）检索模块按约定 key 读取——**不把 Context
对象本身灌进内核**。``extensions`` 的值类型为传输安全的 ``str``，以过 CLI/MCP/HTTP 的
序列化边界。

自适应披露预算经**约定 key** ``extensions[EXT_MAX_TOKENS]``（即 ``"max_tokens"``）承载：
它本质是内核（披露阶段）解释的 int 预算，但作为调用级配置统一收进 ``extensions``，由
API 边界（:class:`~api.memory_api_impl.local_memory_api.LocalMemoryAPI`）取出、解析为
int 后写入 ``RetrievalQuery`` 对应槽位；无此 key（或空串）时披露阶段用默认策略。

鉴权身份 ``identity`` 不在此：它是与「目标范围 + 配置」正交的鉴权关注点，
仍由各 API 方法以独立 keyword-only 参数承载。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .scope import Scope

# extensions 中承载自适应披露预算的约定 key（值为 int 的字符串形式）。
# 由 API 边界解析为 RetrievalQuery.max_tokens；自定义检索模块亦可按此 key 读取。
EXT_MAX_TOKENS = "max_tokens"


@dataclass
class Context:
    scope: Scope = field(default_factory=Scope)  # 操作/检索的目标范围（多租户隔离）
    # 调用级透传配置；内核核心不解释，值须传输安全（str）。约定 key 见 EXT_MAX_TOKENS。
    extensions: dict[str, Any] = field(default_factory=dict)
