"""JiebaTokenizer — 基于 jieba 的中文分词 Tokenizer。

jieba 是最广泛使用的 Python 中文分词库，支持三种分词模式：
- 精确模式（默认）：试图将句子最精确地切开，适合文本分析
- 全模式：把句子中所有可以成词的词语都扫描出来，速度快但不能解决歧义
- 搜索引擎模式：在精确模式基础上，对长词再次切分，提高召回率

本实现默认使用**搜索引擎模式**——兼顾精确度和召回率，
适合关键词索引构建 + 检索 query 分词的场景。

依赖：``jieba``（pip install jieba）。
"""

from __future__ import annotations

from importlib import import_module

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer.base import Tokenizer, TokenizerProducer

logger = get_logger(__name__)


class JiebaTokenizer(Tokenizer):
    """基于 jieba 的中文分词器——搜索引擎模式，兼顾精确与召回。"""

    def __init__(
        self,
        mode: str = "search",
        cut_all: bool = False,
        hmm: bool = True,
    ) -> None:
        """初始化分词器。

        Args:
            mode: 分词模式 — "search"（搜索引擎模式，默认）| "default"（精确模式）
            cut_all: 全模式开关（mode="default" 时生效，设 True 启用全模式）
            hmm: 是否启用 HMM 新词发现（默认 True）
        """
        self._mode = mode
        self._cut_all = cut_all
        self._hmm = hmm
        self._initialized = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def hmm(self) -> bool:
        return self._hmm

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _ensure_initialized(self) -> None:
        """延迟初始化 jieba——首次 tokenize 时才加载词典，避免 import 时等待。"""
        if self._initialized:
            return
        try:
            import_module("jieba")
        except ImportError:
            raise ImportError(
                "JiebaTokenizer requires the 'jieba' package. Install it with: pip install jieba"
            ) from None
        logger.info("JiebaTokenizer: initialized (mode=%s, HMM=%s)", self._mode, self._hmm)
        self._initialized = True

    def plugin_type(self) -> PluginType:
        return PluginType.TOKENIZER

    def health(self) -> None:
        self._ensure_initialized()

    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        self._ensure_initialized()
        import jieba

        if self._mode == "search":
            # 搜索引擎模式：对长词再次切分，提高召回率
            tokens = jieba.cut_for_search(text, HMM=self._hmm)
        else:
            # 精确模式（cut_all=False）或 全模式（cut_all=True）
            tokens = jieba.cut(text, cut_all=self._cut_all, HMM=self._hmm)

        # 过滤空白 token，保留有意义的词
        return [t.strip() for t in tokens if t.strip()]

    def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
        """批量分词——逐条调用 tokenize（jieba 无原生批量接口）。"""
        return [self.tokenize(t) for t in texts]


# -- 注册到 TokenizerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@TokenizerProducer.register("jieba")
def _build(config):
    """Builder: 从配置树节点创建 JiebaTokenizer。

    tokenizer_jieba_mode / tokenizer_jieba_hmm 参数控制分词行为（沿父链回退）。
    """
    return JiebaTokenizer(
        mode=config.get("tokenizer_jieba_mode", "search"),
        hmm=config.get("tokenizer_jieba_hmm", True),
    )
