"""HanlpFeatureExtractor — 基于 HanLP 的 NER 特征抽取。

用 HanLP pipeline 对文本做分词 + 词性标注 + 命名实体识别，产出：
  - keywords：名词/动词/形容词等关键 token（去重保序）
  - entities：HanLP NER 产出的命名实体（PERSON / ORG / LOC / ...）
  - labels：基于词性分布和实体类型的分类标签

降级策略：HanLP 未安装或模型不可用时，回退到简单规则关键词模式
（与 KeywordFeatureExtractor 行为一致），health() 抛 HealthCheckError。

依赖：hanlp（pip install hanlp）+ 预训练模型（HanLP 自动下载）
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import HealthCheckError
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Entity, FeatureSet

logger = get_logger(__name__)

# HanLP NER entity type → agent-memory Entity.type 映射
_HANLP_ENTITY_TYPE_MAP: dict[str, str] = {
    # HanLP 中文 NER 标签
    "nr": "PERSON",       # 人名
    "ns": "LOC",          # 地名
    "nt": "ORG",          # 机构名
    "nz": "PRODUCT",      # 其他专有名词
    "ni": "ORG",          # 机构名
    "nh": "PERSON",       # 人名（变体）
    # HanLP PER/LOC/ORG 风格（部分模型）
    "PER": "PERSON",
    "LOC": "LOC",
    "ORG": "ORG",
    # 英文 NER 标签
    "PERSON": "PERSON",
    "ORGANIZATION": "ORG",
    "LOCATION": "LOC",
    "GPE": "LOC",
    "FACILITY": "LOC",
    "PRODUCT": "PRODUCT",
    "EVENT": "EVENT",
    "DATE": "DATE",
    "TIME": "TIME",
    "MONEY": "MONEY",
}

# HanLP POS tag → 是否作为关键词
# PKU 小写 + CTB 大写（与 CTB9_POS_* 对齐）+ 英文 Penn；同语义标签并存，不改变原有小写/英文过滤意图。
_HANLP_KEYWORD_POS: set[str] = {
    # PKU / 小写风格
    "n",     # 名词
    "nr",    # 人名
    "ns",    # 地名
    "nt",    # 机构名
    "nz",    # 其他专有名词
    "v",     # 动词
    "vd",    # 副动词
    "vn",    # 名动词
    "a",     # 形容词
    "ad",    # 副形词
    "an",    # 名形词
    "d",     # 副词（部分）
    "i",     # 成语
    "j",     # 简略词
    # CTB 大写（CTB9_POS_ELECTRA_SMALL 等）；与上表语义对应
    "NR",    # 人名 ≈ nr
    "NS",    # 地名 ≈ ns
    "NT",    # 机构名 ≈ nt
    "NZ",    # 其他专名 ≈ nz
    "NN",    # 普通名词（亦见英文段）
    "VV",    # 动词 ≈ v
    "VA",    # 形容词性谓词 ≈ a
    "JJ",    # 形容词（亦见英文段）
    "AD",    # 副词 ≈ d
    # 英文 Penn Treebank
    "NNS", "NNP", "NNPS",          # 名词（NN 已在 CTB 段）
    "VB", "VBD", "VBG", "VBN",     # 动词
    "VBP", "VBZ",
    "JJR", "JJS",                  # 形容词（JJ 已在 CTB 段）
    "RB", "RBR", "RBS",            # 副词
}

# 停用词过滤（与 SpacyFeatureExtractor 共用同一套，此处独立定义）
_STOP_WORDS: set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "not", "only", "own", "same", "so", "than", "too", "very", "just",
    "because", "but", "and", "or", "if", "while", "although",
    "it", "its", "he", "she", "they", "them", "their", "this", "that",
    "these", "those", "i", "me", "my", "we", "us", "our", "you", "your",
}


class HanlpFeatureExtractor(FeatureExtractor):
    """HanLP NER + POS 特征抽取：实体识别 + 关键词过滤 + 标签推断。"""

    def __init__(
        self,
        tok_task_name: str = "FINE_ELECTRA_SMALL_ZH",
        task_name: str = "CTB9_POS_ELECTRA_SMALL",
        ner_task_name: str = "MSRA_NER_ELECTRA_SMALL_ZH",
        fallback_to_tokenizer: bool = True,
    ) -> None:
        """初始化 HanlpFeatureExtractor。

        Args:
            tok_task_name: 参数 tok_task_name（str）。
            task_name: 参数 task_name（str）。
            ner_task_name: 参数 ner_task_name（str）。
            fallback_to_tokenizer: 参数 fallback_to_tokenizer（bool）。
        """
        self._tok_task_name = tok_task_name
        self._task_name = task_name
        self._ner_task_name = ner_task_name
        self._fallback_to_tokenizer = fallback_to_tokenizer
        self._tok_task = None
        self._pos_task = None
        self._ner_task = None
        self._available = False

        self._init_hanlp()

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.FEATURE_EXTRACTOR

    def health(self) -> None:
        """执行健康检查。

        Raises:
            HealthCheckError: 执行失败时抛出。
        """
        if not self._available and not self._fallback_to_tokenizer:
            raise HealthCheckError(
                "HanlpFeatureExtractor: HanLP models not available"
            )

    def extract(self, text: str) -> FeatureSet:
        """执行 `extract` 操作。

        Args:
            text: 参数 text（str）。

        Returns:
            返回 FeatureSet。
        """
        if not text.strip():
            return FeatureSet()

        if self._available:
            return self._extract_with_hanlp(text)
        elif self._fallback_to_tokenizer:
            return self._extract_fallback(text)
        else:
            return FeatureSet()

    def _init_hanlp(self) -> None:
        """尝试加载 HanLP 模型；失败时标记为不可用。

        HanLP 2.x STL 调用契约：``tok(text) -> list[str]``，再以 tokens 调用
        ``pos(tokens)`` / ``ner(tokens)``。直接对原文调用 pos/ner 会得到字符级
        嵌套结果，因此 tok 必须可用，否则视为不可用并走 fallback。
        """
        try:
            import hanlp

            # 加载分词任务（STL 上游必需）
            try:
                self._tok_task = hanlp.load(self._tok_task_name)
            except Exception as exc:
                logger.warning(
                    "HanlpFeatureExtractor: tok task '%s' failed to load: %s",
                    self._tok_task_name, exc,
                )
                self._tok_task = None

            # 加载 POS 词性标注任务
            try:
                self._pos_task = hanlp.load(self._task_name)
            except Exception as exc:
                logger.warning(
                    "HanlpFeatureExtractor: POS task '%s' failed to load: %s",
                    self._task_name, exc,
                )
                self._pos_task = None

            # 加载 NER 任务
            try:
                self._ner_task = hanlp.load(self._ner_task_name)
            except Exception as exc:
                logger.warning(
                    "HanlpFeatureExtractor: NER task '%s' failed to load: %s",
                    self._ner_task_name, exc,
                )
                self._ner_task = None

            # tok 必须可用；pos / ner 至少一个可用
            self._available = (
                self._tok_task is not None
                and (self._pos_task is not None or self._ner_task is not None)
            )

            if not self._available:
                logger.warning(
                    "HanlpFeatureExtractor: tok unavailable or both POS/NER "
                    "missing, falling back to simple tokenization"
                )

        except ImportError:
            logger.warning(
                "HanlpFeatureExtractor: HanLP not installed, "
                "falling back to simple tokenization if enabled"
            )
            self._available = False
            self._tok_task = None
            self._pos_task = None
            self._ner_task = None

    def _extract_with_hanlp(self, text: str) -> FeatureSet:
        """HanLP pipeline 提取：POS 关键词 + NER 实体 + 标签推断。

        STL 调用契约：``tok(text)`` → ``pos(tokens)`` / ``ner(tokens)``，
        其中 ``pos`` 返回与 tokens 对齐的 ``list[str]``，``ner`` 返回
        ``[(span, type, start, end), ...]`` 四元组列表。
        """
        keywords: list[str] = []
        entities: list[Entity] = []
        seen_keywords: set[str] = set()

        # --- 分词（STL 上游必需） ---
        tokens: list[str] = []
        if self._tok_task is not None:
            try:
                tok_out = self._tok_task(text)
                if isinstance(tok_out, list):
                    tokens = [str(t) for t in tok_out]
            except Exception:
                logger.warning("HanlpFeatureExtractor: tok task failed, using empty tokens")

        # --- POS 词性标注 ---
        pos_tokens: list[tuple[str, str]] = []
        if self._pos_task is not None and tokens:
            try:
                tags = self._pos_task(tokens)
                if isinstance(tags, list) and len(tags) == len(tokens):
                    pos_tokens = list(zip(tokens, [str(t) for t in tags]))
                else:
                    logger.warning(
                        "HanlpFeatureExtractor: POS returned unexpected shape, "
                        "expected list aligned with tokens"
                    )
            except Exception:
                logger.warning("HanlpFeatureExtractor: POS task failed, using empty pos_tokens")

        # 关键词过滤
        for word, pos in pos_tokens:
            if pos not in _HANLP_KEYWORD_POS:
                continue
            word_stripped = word.strip()
            if not word_stripped or word_stripped in _STOP_WORDS:
                continue
            if len(word_stripped) < 2 and not any(c >= '一' for c in word_stripped):
                continue
            if word_stripped not in seen_keywords:
                seen_keywords.add(word_stripped)
                keywords.append(word_stripped)

        # 如果 POS 未产出关键词，从分词结果中取名词/动词
        if not keywords and pos_tokens:
            for word, pos in pos_tokens:
                word_stripped = word.strip()
                if word_stripped and word_stripped not in _STOP_WORDS and word_stripped not in seen_keywords:
                    seen_keywords.add(word_stripped)
                    keywords.append(word_stripped)

        # --- NER 实体识别 ---
        if self._ner_task is not None and tokens:
            try:
                ner_result = self._ner_task(tokens)
                entities = self._parse_ner_result(ner_result, text)
            except Exception:
                logger.warning("HanlpFeatureExtractor: NER task failed, using empty entities")

        # --- 标签推断 ---
        labels = self._infer_labels(text, keywords, entities)

        return FeatureSet(keywords=keywords, entities=entities, labels=labels)

    def _parse_ner_result(self, result, text: str) -> list[Entity]:
        """解析 HanLP NER 输出，转换为 Entity 列表。"""
        entities: list[Entity] = []

        if result is None:
            return entities

        # HanLP NER 可能返回多种格式：
        # 1. list of triples: [(entity_text, entity_type, start/end), ...]
        # 2. list of HanLP Entity 对象
        # 3. nested list
        if isinstance(result, list):
            for item in result:
                if isinstance(item, tuple) and len(item) >= 2:
                    entity_text = str(item[0])
                    entity_type_raw = str(item[1]) if len(item) >= 2 else ""
                    mapped_type = _HANLP_ENTITY_TYPE_MAP.get(
                        entity_type_raw, entity_type_raw
                    )
                    # HanLP NER 置信度启发式
                    score = min(1.0, 0.5 + 0.1 * min(len(entity_text), 5))
                    entities.append(Entity(
                        text=entity_text,
                        type=mapped_type,
                        score=score,
                    ))
                elif hasattr(item, "entity") or hasattr(item, "text"):
                    # HanLP Entity 对象
                    entity_text = getattr(item, "text", getattr(item, "entity", ""))
                    entity_type_raw = getattr(item, "type", getattr(item, "tag", ""))
                    mapped_type = _HANLP_ENTITY_TYPE_MAP.get(
                        entity_type_raw, entity_type_raw
                    )
                    score = min(1.0, 0.5 + 0.1 * min(len(str(entity_text)), 5))
                    entities.append(Entity(
                        text=str(entity_text),
                        type=mapped_type,
                        score=score,
                    ))

        return entities

    def _extract_fallback(self, text: str) -> FeatureSet:
        """降级提取：HanLP 不可用时，用简单规则提取关键词。"""
        import re

        # 中英文混合关键词提取
        chinese_words = re.findall(r"[一-鿿]{2,}", text)
        english_words = re.findall(r"[a-zA-Z]{3,}", text)

        # 合并去重
        keywords: list[str] = []
        seen: set[str] = set()
        for w in chinese_words + english_words:
            if w not in seen and w not in _STOP_WORDS:
                seen.add(w)
                keywords.append(w)

        keywords = keywords[:20]

        # 简单实体检测：英文大写词
        entities: list[Entity] = []
        for match in re.finditer(r"[A-Z][a-zA-Z]{2,}", text):
            entities.append(Entity(text=match.group(), type="ENTITY", score=0.7))

        # 标签
        labels = self._infer_labels(text, keywords, entities)

        return FeatureSet(keywords=keywords, entities=entities, labels=labels)

    def _infer_labels(
        self,
        text: str,
        keywords: list[str],
        entities: list[Entity],
    ) -> dict[str, str]:
        """基于关键词和实体推断分类标签。"""
        labels: dict[str, str] = {}

        # 情感标签
        positive_words = ("偏好", "喜欢", "喜爱", "满意", "开心", "好", "棒", "优秀")
        negative_words = ("讨厌", "不满", "错误", "报错", "问题", "焦虑", "差", "不好")
        if any(w in text for w in positive_words):
            labels["sentiment"] = "positive"
        elif any(w in text for w in negative_words):
            labels["sentiment"] = "negative"

        # 实体类型标签
        entity_types = {e.type for e in entities}
        if "PERSON" in entity_types:
            labels["has_person"] = "true"
        if "ORG" in entity_types:
            labels["has_org"] = "true"
        if "LOC" in entity_types:
            labels["has_location"] = "true"

        # 语言标签
        has_chinese = any("一" <= c <= "鿿" for c in text)
        has_english = any(c.isascii() and c.isalpha() for c in text)
        if has_chinese and has_english:
            labels["language"] = "mixed"
        elif has_chinese:
            labels["language"] = "zh"
        elif has_english:
            labels["language"] = "en"

        return labels


# -- 注册到 FeatureExtractorProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@FeatureExtractorProducer.register("hanlp")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return HanlpFeatureExtractor(
        tok_task_name=config.get("feature_extractor_hanlp_tok_task", "FINE_ELECTRA_SMALL_ZH"),
        task_name=config.get("feature_extractor_hanlp_task", "CTB9_POS_ELECTRA_SMALL"),
        ner_task_name=config.get("feature_extractor_hanlp_ner_task", "MSRA_NER_ELECTRA_SMALL_ZH"),
        fallback_to_tokenizer=config.get("feature_extractor_hanlp_fallback", True),
    )
