# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~common.llm.base.LLM`——确定性回显桩。

无外部模型/服务：``chat`` 返回最后一条 user 消息内容（``generate`` 即回显 prompt）。
用作 query 改写 / 摘要 / 冲突消解的可插拔占位——上线替换为真实 vLLM/OpenAI 兼容
后端即可，调用方 prompt 不变。回显语义下「改写」等价于原文，不引入随机性，便于
测试与复现。
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.type_def import ChatMessage


class EchoLLM(LLM):
    """回显式 LLM 桩：返回最后一条 user 消息内容。"""

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del options
        for msg in reversed(messages):
            if msg.role == "user":
                return _content_text(msg.content)
        return _content_text(messages[-1].content) if messages else ""


def _content_text(content: str | list[dict]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if part.get("type") == "text"
    )


# -- 注册到 LlmProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@LlmProducer.register("echo")
def _build(config):
    return EchoLLM()
