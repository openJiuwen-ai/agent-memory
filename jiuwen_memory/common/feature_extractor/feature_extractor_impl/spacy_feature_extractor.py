"""SpacyFeatureExtractor — 基于 spaCy 的 NER 特征抽取。

用 spaCy pipeline 对文本做分词 + 词性标注 + 命名实体识别，产出：
  - keywords：名词/动词/形容词等关键 token（去重保序）
  - entities：spaCy NER 产出的命名实体（PERSON / ORG / LOC / GPE / ...）
  - labels：基于词性分布的粗分类标签（sentiment / domain 等）

降级策略：spaCy 未安装或模型不可用时，回退到 Tokenizer 分词关键词模式
（与 KeywordFeatureExtractor 行为一致），health() 抛 HealthCheckError。

依赖：spaCy（pip install spacy）+ 语言模型（如 zh_core_web_sm / en_core_web_sm）
"""

from __future__ import annotations

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import HealthCheckError
from jiuwen_memory.common.feature_extractor.base import FeatureExtractor, FeatureExtractorProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Entity, FeatureSet

logger = get_logger(__name__)

# spaCy NER entity type → agent-memory Entity.type 映射
_SPACY_ENTITY_TYPE_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "GPE": "LOC",  # Geo-Political Entity → LOC
    "LOC": "LOC",
    "FAC": "LOC",  # Facility
    "PRODUCT": "PRODUCT",
    "EVENT": "EVENT",
    "WORK_OF_ART": "WORK_OF_ART",
    "LAW": "LAW",
    "LANGUAGE": "LANGUAGE",
    "DATE": "DATE",
    "TIME": "TIME",
    "MONEY": "MONEY",
    "QUANTITY": "QUANTITY",
    "PERCENT": "PERCENT",
    # spaCy 中文模型的特殊类型
    "NAME": "PERSON",
    "ORGANIZATION": "ORG",
    "LOCATION": "LOC",
    "COMPANY": "ORG",
}

# spaCy POS tag → 是否作为关键词
_KEYWORD_POS: set[str] = {
    # 通用
    "NOUN",
    "PROPN",
    "VERB",
    "ADJ",
    "ADV",
    # 中文 spaCy 可能用的细粒度标签
    "n",
    "nr",
    "ns",
    "nt",
    "nz",  # 名词类
    "v",
    "vd",
    "vn",  # 动词类
    "a",
    "ad",
    "an",  # 形容词类
    "d",  # 副词
}

# 停用词过滤（常见虚词/代词）
_STOP_WORDS: set[str] = {
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "一个",
    "上",
    "也",
    "很",
    "到",
    "说",
    "要",
    "去",
    "你",
    "会",
    "着",
    "没有",
    "看",
    "好",
    "自己",
    "这",
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "out",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "every",
    "both",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "because",
    "but",
    "and",
    "or",
    "if",
    "while",
    "although",
    "it",
    "its",
    "he",
    "she",
    "they",
    "them",
    "their",
    "this",
    "that",
    "these",
    "those",
    "i",
    "me",
    "my",
    "we",
    "us",
    "our",
    "you",
    "your",
}


class SpacyFeatureExtractor(FeatureExtractor):
    """spaCy NER + POS 特征抽取：实体识别 + 关键词过滤 + 标签推断。"""

    def __init__(
        self,
        model_name: str = "zh_core_web_sm",
        fallback_to_tokenizer: bool = True,
    ) -> None:
        self._model_name = model_name
        self._fallback_to_tokenizer = fallback_to_tokenizer
        self._nlp = None
        self._available = False

        self._init_spacy()

    @staticmethod
    def _get_lang_from_model_name(model_name: str) -> str:
        """从模型名推断语言代码：zh_core_web_sm → zh, en_core_web_sm → en。"""
        if model_name.startswith("zh"):
            return "zh"
        elif model_name.startswith("en"):
            return "en"
        elif model_name.startswith("ja"):
            return "ja"
        elif model_name.startswith("de"):
            return "de"
        elif model_name.startswith("fr"):
            return "fr"
        # 默认中文（项目主要面向中文场景）
        return "zh"

    def plugin_type(self) -> PluginType:
        return PluginType.FEATURE_EXTRACTOR

    def health(self) -> None:
        if not self._available:
            raise HealthCheckError(
                f"SpacyFeatureExtractor: spaCy model '{self._model_name}' not available"
            )

    def extract(self, text: str) -> FeatureSet:
        if not text.strip():
            logger.debug("SpacyFeatureExtractor: input text is empty, returning empty FeatureSet")
            return FeatureSet()

        logger.info(
            "SpacyFeatureExtractor: extracting features for text (%d chars): %s",
            len(text),
            text[:200],
        )

        if self._available and self._nlp is not None:
            fs = self._extract_with_spacy(text)
        elif self._fallback_to_tokenizer:
            logger.info("SpacyFeatureExtractor: spaCy unavailable, using fallback tokenizer")
            fs = self._extract_fallback(text)
        else:
            logger.warning(
                "SpacyFeatureExtractor: spaCy unavailable and fallback disabled, "
                "returning empty FeatureSet"
            )
            return FeatureSet()

        logger.info(
            "SpacyFeatureExtractor: extraction result — keywords=%s, entities=%s, labels=%s",
            fs.keywords,
            [{"text": e.text, "type": e.type, "score": e.score} for e in fs.entities],
            fs.labels,
        )
        return fs

    def _init_spacy(self) -> None:
        """尝试加载 spaCy 模型；失败时标记为不可用。"""
        try:
            import spacy
        except ImportError:
            logger.warning(
                "SpacyFeatureExtractor: spaCy not installed, "
                "falling back to simple tokenization if enabled"
            )
            self._available = False
            self._nlp = None
            return

        try:
            self._nlp = spacy.load(self._model_name)
            self._available = True
        except OSError:
            logger.warning(
                "SpacyFeatureExtractor: model %s not found, "
                "falling back to simple tokenization if enabled",
                self._model_name,
            )
            self._available = False
            self._nlp = None

    def _extract_with_spacy(self, text: str) -> FeatureSet:
        """spaCy pipeline 提取：NER + POS 关键词 + 标签推断。"""
        doc = self._nlp(text)

        logger.debug("SpacyFeatureExtractor: spaCy doc tokens=%d, ents=%d", len(doc), len(doc.ents))

        # --- 关键词：基于 POS 过滤 ---
        keywords: list[str] = []
        seen: set[str] = set()
        skipped_reasons: dict[str, int] = {}  # 统计跳过原因
        for token in doc:
            # 跳过停用词、短词、纯标点
            if token.pos_ not in _KEYWORD_POS and token.tag_ not in _KEYWORD_POS:
                skipped_reasons["pos_not_keyword"] = skipped_reasons.get("pos_not_keyword", 0) + 1
                continue
            word = token.text.strip()
            if not word or word in _STOP_WORDS:
                skipped_reasons["stopword_or_empty"] = (
                    skipped_reasons.get("stopword_or_empty", 0) + 1
                )
                continue
            if len(word) < 2 and not any(c >= "一" for c in word):
                # 单字符非中文 → 跳过
                skipped_reasons["short_non_cjk"] = skipped_reasons.get("short_non_cjk", 0) + 1
                continue
            if word not in seen:
                seen.add(word)
                keywords.append(word)

        logger.debug(
            "SpacyFeatureExtractor: keyword extraction — accepted=%d, skipped=%s",
            len(keywords),
            skipped_reasons,
        )

        # --- 实体：NER 产出 ---
        entities: list[Entity] = []
        for ent in doc.ents:
            mapped_type = _SPACY_ENTITY_TYPE_MAP.get(ent.label_, ent.label_)
            # NER 置信度：spaCy 不直接提供，用实体长度和上下文启发式估算
            score = min(1.0, 0.5 + 0.1 * min(len(ent.text), 5))
            entities.append(
                Entity(
                    text=ent.text,
                    type=mapped_type,
                    score=score,
                )
            )
            logger.debug(
                "SpacyFeatureExtractor: NER entity - text=%r, spacy_label=%r, "
                "mapped_type=%r, score=%.2f",
                ent.text,
                ent.label_,
                mapped_type,
                score,
            )

        # --- 标签：基于关键词和实体的粗分类 ---
        labels = self._infer_labels(text, keywords, entities)

        return FeatureSet(keywords=keywords, entities=entities, labels=labels)

    def _extract_fallback(self, text: str) -> FeatureSet:
        """降级提取：spaCy 不可用时，用简单规则提取关键词。"""
        import re

        logger.info(
            "SpacyFeatureExtractor: fallback extraction for text (%d chars): %s",
            len(text),
            text[:200],
        )

        # 中英文混合关键词提取：中文长词 + 双字组合，英文 3 字符以上单词。
        chinese_words = re.findall(r"[一-鿿]{2,}", text)
        chinese_chars = re.findall(r"[一-鿿]", text)
        for i in range(len(chinese_chars) - 1):
            chinese_words.append(chinese_chars[i] + chinese_chars[i + 1])

        # 英文词
        english_words = re.findall(r"[a-zA-Z]{3,}", text)

        logger.debug(
            "SpacyFeatureExtractor: fallback — chinese_words=%d, english_words=%d",
            len(chinese_words),
            len(english_words),
        )

        # 合并去重
        all_keywords = []
        seen: set[str] = set()
        for w in chinese_words + english_words:
            if w not in seen and w not in _STOP_WORDS:
                seen.add(w)
                all_keywords.append(w)

        # 限制关键词数量
        keywords = all_keywords[:20]

        # 简单实体检测：英文大写词
        entities = []
        for match in re.finditer(r"[A-Z][a-zA-Z]{2,}", text):
            entity = Entity(text=match.group(), type="ENTITY", score=0.7)
            entities.append(entity)
            logger.debug(
                "SpacyFeatureExtractor: fallback entity — text='%s', type='ENTITY', score=0.7",
                match.group(),
            )

        # 标签
        labels = self._infer_labels(text, keywords, entities)

        logger.info(
            "SpacyFeatureExtractor: fallback result — keywords=%s, entities=%s, labels=%s",
            keywords,
            [{"text": e.text, "type": e.type, "score": e.score} for e in entities],
            labels,
        )

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


@FeatureExtractorProducer.register("spacy")
def _build(config):
    return SpacyFeatureExtractor(
        model_name=config.get("feature_extractor_spacy_model", "zh_core_web_sm"),
        fallback_to_tokenizer=config.get("feature_extractor_spacy_fallback", True),
    )
