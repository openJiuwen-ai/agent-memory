"""最小实现：:class:`~common.tokenizer.base.Tokenizer` 的纯内存分词器。

无外部依赖：小写化后，拉丁字母/数字按连续串成词、CJK 逐字成元（unigram），
避免整段汉字粘成一个 token 导致关键词检索召回错位。构建建索引与检索 query
共用同一实例即保证同词表。
"""

from __future__ import annotations

import re

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer.base import Tokenizer, TokenizerProducer

logger = get_logger(__name__)

# 拉丁字母/数字按连续串成词；CJK 逐字成元（unigram）。
_TOKEN_RE = re.compile(r"[0-9a-z]+|[一-鿿]")


class WhitespaceTokenizer(Tokenizer):
    """小写化分词：拉丁/数字成词、汉字逐字成元的极简分词器。"""

    def plugin_type(self) -> PluginType:
        return PluginType.TOKENIZER

    def health(self) -> None:
        return None

    def tokenize(self, text: str) -> list[str]:
        tokens = _TOKEN_RE.findall(text.lower())
        logger.info("WhitespaceTokenizer: tokenized %d chars into %d tokens", len(text), len(tokens))
        return tokens


# -- 注册到 TokenizerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@TokenizerProducer.register("whitespace")
def _build(config):
    return WhitespaceTokenizer()
