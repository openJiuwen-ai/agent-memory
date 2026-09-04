# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scope — 多维作用域（架构 §3.2）。

``org > space > user/agent > session`` 五维归属，统一支撑隔离（多租户、
单 Agent 私有）与共享（跨 Agent 共享池）；检索/写入默认在 scope 内。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Scope:
    org: str = ""  # 组织/租户
    space: str = field(default="", kw_only=True)  # 全局唯一的逻辑隔离空间标识
    user: str = ""  # 用户
    agent: str = ""  # Agent 标识
    session: str = ""  # 会话标识


# 内核自带的归属坐标实体名（F07）。取值以调用方身份为准，部署声明项不得与之重名——
# 重名即接入方传入的取值覆盖内核由身份推导的权威取值（判定表加载期第 12 条校验）。
#
# **三项取值必须是 :class:`Scope` 的字段名**，因此紧贴该类定义：折算函数
# （``common.security.principal.kernel_coords``）按本元组逐项 ``getattr`` 取身份取值，
# 取值与字段名对不上即 ``AttributeError``。新增第四项内核坐标时须同时给 ``Scope`` 加同名
# 字段。落本模块而非构建层，是因为消费方跨安全层与构建层两侧，而安全层不得反向依赖构建层。
KERNEL_COORD_KEYS: tuple[str, ...] = ("user", "agent", "session")


def space_id_from_scope(scope: Scope) -> str:
    """生成 entity 索引的 routing 与文档字段值。

    选 scope.space（设计隔离单元，同 space 文档聚簇一个分片，查询只扫一个分片）。
    本地栈 scope.space 为空（InMemoryEngine 要求 space==''），用 scope.org 兜底
    避免全落一个分片；再空落 "default" 避免 routing 空串。
    ES 不要求 routing 是 UUID，str 即可。

    跨层共享纯函数（storage/retrieval/construction 三层 entity 索引隔离共用），
    归口 type_def，与 ``Scope`` 同处。
    """
    if scope.space:
        return scope.space
    if scope.org:
        return scope.org
    return "default"
