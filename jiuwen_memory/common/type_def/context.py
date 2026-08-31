# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Context — 调用上下文：目标范围 + 调用级透传配置。

把「在谁的范围内」（``scope``）与「调用级透传配置」（``extensions``）打包成调用端
便利结构。**Context 只活在接口层**：API 方法在边界处把它拆开——target ``scope`` 照旧
作为独立轴下推（鉴权/检索），``extensions`` 经 API 边界写入调用级 options、顺 parser
进 ``ParsedQuery``，透传给（用户自定义的）检索模块按约定 key 读取——**不把 Context
对象本身灌进内核**。``extensions`` 的值类型为 ``Any``：本地调用可透传运行时对象，
CLI/MCP/HTTP 等序列化边界由接入层自行限制为可传输值。

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

# extensions 中把 search 转为跨空间检索的约定 key，取值 ``list[str]``。
#
# **判据取键的有无，不取取值形态**：键不在即单空间检索，行为与本特性之前一字不差；键在
# 即跨空间，取值为空列表表示「调用方能读的全部空间」（由主体反查索引给出），非空即显式
# 候选集。取值判空则「查我能读的全部」这层意图无法表达，只能退回缺省状态触发。
#
# 与 ``EXT_MAX_TOKENS`` 同一处置：由 API 边界取出并从透传 options 中移除，不随 parser
# 下传给自定义检索模块——它是内核解释的编排开关，不是检索模块的入参。
EXT_SPACES = "spaces"


@dataclass
class Context:
    scope: Scope = field(default_factory=Scope)  # 操作/检索的目标范围（多租户隔离）
    # 调用级透传配置；内核核心不解释。
    # 三个约定 key 由 API 边界解释并移除：EXT_MAX_TOKENS、EXT_SPACES、"coords"。
    extensions: dict[str, Any] = field(default_factory=dict)
