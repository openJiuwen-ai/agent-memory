"""QueryParser — 查询理解。

把自然语言 query 解析为结构化的 :class:`ParsedQuery`，供各召回通道
直接消费：意图识别与 query 改写（LLM）、分词（Tokenizer，关键词通道）、
实体/关键词抽取（FeatureExtractor，图通道）、向量化（Embedder，向量
通道）、时间约束解析（时序过滤），并按配置给出建议启用的召回通道。
必须与构建侧使用同一套共享插件，保证同词表、同向量空间。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory

from .base import RetrievalOperator
from .types import ParsedQuery, RetrievalQuery


class QueryParserProducer(Factory):
    """QueryParser 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``query_parser_impl`` 下以 ``@QueryParserProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由 :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "query_parser"


class QueryParser(RetrievalOperator):
    @abstractmethod
    def parse(self, query: RetrievalQuery) -> ParsedQuery:
        """将检索请求解析为结构化查询表示。"""
