"""ChatMessage — LLM 对话补全请求中的一轮消息。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatMessage:
    role: str = "user"  # 角色：system | user | assistant
    content: str = ""  # 消息文本
