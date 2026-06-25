"""query_parser_impl 实现集：工厂 QueryParserProducer + 各实现。

import 各实现模块即触发其 ``@QueryParserProducer.register(...)`` 自注册；本包只对外暴露工厂 QueryParserProducer。
"""

from importlib import import_module

from retrieval.query_parser import QueryParserProducer

import_module(".simple_query_parser", __name__)

__all__ = ["QueryParserProducer"]
