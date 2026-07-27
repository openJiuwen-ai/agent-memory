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
