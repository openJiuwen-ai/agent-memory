"""ChatMessage — LLM 对话补全请求中的一轮消息。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChatMessage:
    role: str = "user"  # 角色：system | user | assistant
    # OpenAI-compatible providers accept plain text or typed multimodal parts.
    content: str | list[dict[str, Any]] = ""
