"""Scope — 多维作用域（架构 §3.2）。

``org > space > user/agent > session`` 五维归属，统一支撑隔离（多租户、
单 Agent 私有）与共享（跨 Agent 共享池）；检索/写入默认在 scope 内。

**frozen=True（验收第三次 P2-1）**：Scope 是身份/隔离的值对象，可变性是安全
缺陷--签发 key 后改原 actor 的 org，会让已签发 key 的身份跟着变（越权）。改某维
用 ``dataclasses.replace(scope, org=...)`` 返回新值，不原地修改。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scope:
    org: str = ""  # 组织/租户
    space: str = field(default="", kw_only=True)  # 全局唯一的逻辑隔离空间标识
    user: str = ""  # 用户
    agent: str = ""  # Agent 标识
    session: str = ""  # 会话标识
