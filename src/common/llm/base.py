"""LLM — 大模型调用能力（vLLM 部署 / OpenAI 兼容 chat 后端）。

**共用说明**：通用生成端口。构建层用于信息提取/摘要/抽象升华；检索层
用于 query 改写/答案合成；自演进用于冲突消解。任务相关的 prompt 由
调用方负责——本接口保持通用，才可被各层复用。
"""

from __future__ import annotations

from abc import abstractmethod

from ..factory.factory import Factory
from ..base import Plugin
from ..type_def import ChatMessage


class LlmProducer(Factory):
    """LLM 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``llm_impl`` 下以 ``@LlmProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "llm"


class LLM(Plugin):
    @abstractmethod
    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        """执行一次对话补全，返回助手回复文本。

        ``options`` 携带生成参数（``temperature``、``max_tokens`` 等），
        后端忽略不认识的键。"""

    def generate(self, prompt: str, **options: object) -> str:
        """单 prompt 便捷方法；默认包装为一轮 user 消息调用 :meth:`chat`。"""
        return self.chat([ChatMessage(role="user", content=prompt)], **options)
