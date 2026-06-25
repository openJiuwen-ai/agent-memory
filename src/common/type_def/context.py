"""Context — 调用上下文：目标范围 + 内核一等调用级配置 + 用户自定义透传配置。

把「在谁的范围内」（``scope``）、「内核解释的调用级配置」（如 ``max_tokens`` 自适应
披露预算）与「用户自定义透传配置」（``extensions``）打包成调用端便利结构。
**Context 只活在接口层**：API 方法在边界处把它拆开——target ``scope`` 照旧作为独立轴
下推（鉴权/检索），``max_tokens`` 等一等字段写入 ``RetrievalQuery`` 对应槽位由内核解释，
``extensions`` 写入调用级 options 段、顺 parser 进 ``ParsedQuery``，透传给（用户自定义的）
检索模块按约定 key 读取——**不把 Context 对象本身灌进内核**。如此既保留 scope 的独立轴
设计，又让 ``extensions`` 过得了 CLI/MCP/HTTP 的序列化边界（故值类型为传输安全的 ``str``）。

``max_tokens`` 与 ``extensions`` 性质不同：前者是**内核解释**的披露预算（typed ``int``），
后者是**内核不解释**的不透明透传——故各占独立字段，不混为一谈。

鉴权身份 ``identity`` 不在此：它是与「目标范围 + 配置」正交的鉴权关注点，
仍由各 API 方法以独立 keyword-only 参数承载。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scope import Scope


@dataclass
class Context:
    scope: Scope = field(default_factory=Scope)  # 操作/检索的目标范围（多租户隔离）
    max_tokens: int | None = None  # 自适应披露预算（内核解释）；None = 用 discloser 默认策略
    extensions: dict[str, str] = field(default_factory=dict)  # 用户自定义透传配置；内核核心不解释，自定义检索模块按约定 key 读取（值须传输安全）
