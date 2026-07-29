"""最小实现：:class:`~retrieval.query_parser.QueryParser`。

把 ``RetrievalQuery`` 解析为 ``ParsedQuery``：用注入的 Tokenizer 分词（与索引侧同
一实例 → 同词表）；若注入了 LLM，则用它改写 query（``rewritten``）；若注入了
Embedder，则对 query 向量化（同一向量空间）并启用 VECTOR 通道；若注入了
FeatureExtractor，则抽实体/关键词（供图通道找种子）。``filters`` 透传为硬过滤、
``as_of`` 透传为 valid-time 回溯点；时间约束经规则解析为 event-time 窗口。
"""

from __future__ import annotations

from common.embedder.base import Embedder, EmbedderProducer
from common.factory.factory import Factory
from common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from common.llm.base import LLM, LlmProducer
from common.tokenizer import Tokenizer
from common.tokenizer.base import TokenizerProducer
from retrieval.base import RetrievalOperatorType
from retrieval.query_parser import QueryParser, QueryParserProducer
from retrieval.types import ParsedQuery, RecallChannel, RetrievalQuery

from .sanitize import sanitize_query
from .time_parse import parse_time


class SimpleQueryParser(QueryParser):
    """分词（+可选 LLM 改写 / 向量化 / 特征抽取）版查询理解。"""

    def __init__(
        self,
        tokenizer: Tokenizer,
        embedder: Embedder | None = None,
        llm: LLM | None = None,
        feature_extractor: FeatureExtractor | None = None,
        *,
        sanitize: bool = False,
        strip_code_fences: bool = False,
    ) -> None:
        self._tokenizer = tokenizer
        self._embedder = embedder
        self._llm = llm
        self._features = feature_extractor
        self._sanitize = sanitize
        self._strip_code_fences = strip_code_fences

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.QUERY_PARSER

    def health(self) -> None:
        return None

    def parse(self, query: RetrievalQuery) -> ParsedQuery:
        text = (
            sanitize_query(query.text, strip_code_fences=self._strip_code_fences)
            if self._sanitize
            else query.text
        )
        rewritten = self._llm.generate(text) if self._llm is not None else text
        tokens = self._tokenizer.tokenize(rewritten)
        # 关键词与图通道都靠 token/关键词驱动；向量通道需有 Embedder。
        channels = [RecallChannel.KEYWORD, RecallChannel.GRAPH]
        vector: list[float] = []
        if self._embedder is not None:
            vector = self._embedder.embed_query(rewritten)
            channels.append(RecallChannel.VECTOR)

        # 特征抽取：实体供图通道找种子，关键词并入 token 集（去重保序）。
        keywords = list(tokens)
        entities = []
        if self._features is not None:
            fs = self._features.extract(rewritten)
            entities = fs.entities
            seen = set(keywords)
            for kw in fs.keywords:
                if kw not in seen:
                    keywords.append(kw)
                    seen.add(kw)

        # 时间约束解析（event-time 窗口）：规则版，留 LLM 钩子（默认不启用）。
        time_from, time_to = parse_time(text)

        return ParsedQuery(
            raw=text,
            rewritten=rewritten,
            tokens=tokens,
            keywords=keywords,
            entities=entities,
            vector=vector,
            scalar_filters=query.filters,
            as_of=query.as_of,
            time_from=time_from,
            time_to=time_to,
            channels=channels,
        )


# -- 注册到 QueryParserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@QueryParserProducer.register("simple")
def _build(config):
    # tokenizer / embedder 与索引侧共享同一实例（同词表 / 同向量空间）；无向量能力时不接 embedder。
    tokenizer = TokenizerProducer.dep(config, default="whitespace")
    embedder = (
        EmbedderProducer.dep(config, default="hashing")
        if config.get("vector_enabled", True)
        else None
    )
    llm = LlmProducer.dep(config, default="echo")
    features = FeatureExtractorProducer.dep(config, default="keyword")
    return SimpleQueryParser(
        tokenizer,
        embedder,
        llm,
        features,
        sanitize=Factory.cfg_get(config, "sanitize_enabled", True),
        strip_code_fences=Factory.cfg_get(config, "sanitize_strip_code", False),
    )
