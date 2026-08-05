"""FeatureExtractor 单元测试。

使用 pytest 的 skipif 标记处理 spaCy/HanLP 未安装的情况。
未安装时只测试降级模式（fallback）和基础契约。
"""

import re
import sys
from importlib import import_module

from common.base import PluginType
from common.type_def import FeatureSet

# ---------------------------------------------------------------------------
# 检查 spaCy / HanLP 是否可用
# ---------------------------------------------------------------------------

try:
    _spacy = import_module("spacy")
except ImportError:
    SPACY_MODEL_AVAILABLE = False
else:
    try:
        _spacy.load("zh_core_web_sm")
        SPACY_MODEL_AVAILABLE = True
    except OSError:
        SPACY_MODEL_AVAILABLE = False

try:
    import_module("hanlp")
    HANLP_AVAILABLE = True
except ImportError:
    HANLP_AVAILABLE = False


# ---------------------------------------------------------------------------
# SimpleTokenizer（与 RecursiveChunker 测试一致）
# ---------------------------------------------------------------------------


class SimpleTokenizer:
    """SimpleTokenizer：按空格+标点分词（仅用于 KeywordFeatureExtractor）。"""

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zA-Z]+|\w", text)


# ---------------------------------------------------------------------------
# Helper: 创建测试实例
# ---------------------------------------------------------------------------


def _make_keyword_extractor():
    from common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
        KeywordFeatureExtractor,
    )

    return KeywordFeatureExtractor(tokenizer=SimpleTokenizer())


def _make_spacy_extractor(model_name="zh_core_web_sm"):
    from common.feature_extractor.feature_extractor_impl.spacy_feature_extractor import (
        SpacyFeatureExtractor,
    )

    return SpacyFeatureExtractor(model_name=model_name, fallback_to_tokenizer=True)


def _make_spacy_extractor_no_fallback(model_name="zh_core_web_sm"):
    from common.feature_extractor.feature_extractor_impl.spacy_feature_extractor import (
        SpacyFeatureExtractor,
    )

    return SpacyFeatureExtractor(model_name=model_name, fallback_to_tokenizer=False)


def _install_fake_spacy_without_model(monkeypatch):
    class FakeSpacy:
        @staticmethod
        def load(model_name: str):
            raise OSError(f"model {model_name} not found")

    monkeypatch.setitem(sys.modules, "spacy", FakeSpacy)


def _make_hanlp_extractor():
    from common.feature_extractor.feature_extractor_impl.hanlp_feature_extractor import (
        HanlpFeatureExtractor,
    )

    return HanlpFeatureExtractor(
        tok_task_name="FINE_ELECTRA_SMALL_ZH",
        task_name="CTB9_POS_ELECTRA_SMALL",
        ner_task_name="MSRA_NER_ELECTRA_SMALL_ZH",
        fallback_to_tokenizer=True,
    )


def _make_hanlp_extractor_no_fallback():
    from common.feature_extractor.feature_extractor_impl.hanlp_feature_extractor import (
        HanlpFeatureExtractor,
    )

    return HanlpFeatureExtractor(
        tok_task_name="FINE_ELECTRA_SMALL_ZH",
        task_name="CTB9_POS_ELECTRA_SMALL",
        ner_task_name="MSRA_NER_ELECTRA_SMALL_ZH",
        fallback_to_tokenizer=False,
    )


# ---------------------------------------------------------------------------
# KeywordFeatureExtractor 测试
# ---------------------------------------------------------------------------


def test_keyword_plugin_type():
    """KeywordFeatureExtractor.plugin_type() 返回 FEATURE_EXTRACTOR。"""
    fe = _make_keyword_extractor()
    assert fe.plugin_type() == PluginType.FEATURE_EXTRACTOR


def test_keyword_health():
    """KeywordFeatureExtractor.health() 正常时不抛异常。"""
    fe = _make_keyword_extractor()
    assert fe.health() is None


def test_keyword_extract_basic():
    """KeywordFeatureExtractor：英文关键词提取。"""
    fe = _make_keyword_extractor()
    result = fe.extract("Python is a programming language")
    assert isinstance(result, FeatureSet)
    assert len(result.keywords) > 0
    assert "Python" in result.keywords


def test_keyword_extract_entities():
    """KeywordFeatureExtractor：拉丁长词 → TERM 实体。"""
    fe = _make_keyword_extractor()
    result = fe.extract("Python is a programming language")
    # 长度≥3 的 ASCII 词应成为 TERM 实体
    term_texts = [e.text for e in result.entities if e.type == "TERM"]
    assert "Python" in term_texts
    assert "programming" in term_texts or "language" in term_texts


def test_keyword_extract_empty():
    """KeywordFeatureExtractor：空文本 → 空 FeatureSet。"""
    fe = _make_keyword_extractor()
    result = fe.extract("")
    assert result.keywords == []
    assert result.entities == []


# ---------------------------------------------------------------------------
# SpacyFeatureExtractor 测试
# ---------------------------------------------------------------------------


def test_spacy_plugin_type():
    """SpacyFeatureExtractor.plugin_type() 返回 FEATURE_EXTRACTOR。"""
    fe = _make_spacy_extractor()
    assert fe.plugin_type() == PluginType.FEATURE_EXTRACTOR


def test_spacy_fallback_extract_basic():
    """SpacyFeatureExtractor 降级模式：提取中文关键词。"""
    fe = _make_spacy_extractor()
    result = fe.extract("用户偏好用 Python 写代码")
    assert isinstance(result, FeatureSet)
    # 降级模式应产出关键词
    assert len(result.keywords) > 0


def test_spacy_fallback_extract_empty():
    """SpacyFeatureExtractor 降级模式：空文本 → 空 FeatureSet。"""
    fe = _make_spacy_extractor()
    result = fe.extract("")
    assert result.keywords == []
    assert result.entities == []


def test_spacy_fallback_labels():
    """SpacyFeatureExtractor 降级模式：偏好词 → sentiment=positive。"""
    fe = _make_spacy_extractor()
    result = fe.extract("用户偏好用 Python")
    assert result.labels.get("sentiment") == "positive"


def test_spacy_fallback_negative_labels():
    """SpacyFeatureExtractor 降级模式：错误词 → sentiment=negative。"""
    fe = _make_spacy_extractor()
    result = fe.extract("系统报错了，内存泄漏问题")
    assert result.labels.get("sentiment") == "negative"


def test_spacy_fallback_language_labels():
    """SpacyFeatureExtractor 降级模式：中英文混合 → language=mixed。"""
    fe = _make_spacy_extractor()
    result = fe.extract("用户偏好 Python 编程")
    assert result.labels.get("language") == "mixed"


def test_spacy_missing_model_uses_fallback(monkeypatch):
    """SpacyFeatureExtractor：spaCy 包存在但模型缺失时应走 fallback。"""
    _install_fake_spacy_without_model(monkeypatch)
    fe = _make_spacy_extractor()

    result = fe.extract("用户偏好用 Python 写代码")

    assert len(result.keywords) > 0
    assert "Python" in result.keywords
    assert result.labels.get("language") == "mixed"


def test_spacy_no_fallback_health_fails():
    """SpacyFeatureExtractor 无降级：spaCy 模型不可用时 health() 抛异常。"""
    fe = _make_spacy_extractor_no_fallback()
    if not SPACY_MODEL_AVAILABLE:
        from common.errors import HealthCheckError

        try:
            fe.health()
        except HealthCheckError as exc:
            assert exc is not None, "health should raise HealthCheckError when unavailable"
        else:
            assert False, "health should raise HealthCheckError when model is unavailable"


def test_spacy_no_fallback_extract_empty_result():
    """SpacyFeatureExtractor 无降级：spaCy 模型不可用时 extract() 返回空 FeatureSet。"""
    fe = _make_spacy_extractor_no_fallback()
    if not SPACY_MODEL_AVAILABLE:
        result = fe.extract("用户偏好 Python")
        assert result.keywords == []
        assert result.entities == []


def test_spacy_factory_registration():
    """SpacyFeatureExtractor 通过工厂注册。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer

    assert "spacy" in FeatureExtractorProducer.known()


# ---------------------------------------------------------------------------
# spaCy 可用时的深度测试
# ---------------------------------------------------------------------------


def test_spacy_extract_with_model():
    """SpacyFeatureExtractor spaCy 可用时：完整 NER + POS 提取。"""
    if not SPACY_MODEL_AVAILABLE:
        return  # spaCy 模型未安装时跳过

    fe = _make_spacy_extractor()
    result = fe.extract("Barack Obama visited Beijing last week")
    assert isinstance(result, FeatureSet)
    # 应有关键词
    assert len(result.keywords) > 0
    # 应有实体（spaCy 能识别 PERSON/LOC）
    assert len(result.entities) > 0


def test_spacy_extract_chinese():
    """SpacyFeatureExtractor spaCy 可用时：中文文本提取。"""
    if not SPACY_MODEL_AVAILABLE:
        return

    fe = _make_spacy_extractor()
    result = fe.extract("张三在北京的清华大学工作")
    assert isinstance(result, FeatureSet)
    assert len(result.keywords) > 0


def test_spacy_health_ok_when_available():
    """SpacyFeatureExtractor spaCy 可用时：health() 返回 None。"""
    if not SPACY_MODEL_AVAILABLE:
        return

    fe = _make_spacy_extractor()
    assert fe.health() is None


# ---------------------------------------------------------------------------
# HanlpFeatureExtractor 测试
# ---------------------------------------------------------------------------


def test_hanlp_plugin_type():
    """HanlpFeatureExtractor.plugin_type() 返回 FEATURE_EXTRACTOR。"""
    fe = _make_hanlp_extractor()
    assert fe.plugin_type() == PluginType.FEATURE_EXTRACTOR


def test_hanlp_fallback_extract_basic():
    """HanlpFeatureExtractor 降级模式：提取中文关键词。"""
    fe = _make_hanlp_extractor()
    result = fe.extract("用户偏好用 Python 写代码")
    assert isinstance(result, FeatureSet)
    assert len(result.keywords) > 0


def test_hanlp_fallback_extract_empty():
    """HanlpFeatureExtractor 降级模式：空文本 → 空 FeatureSet。"""
    fe = _make_hanlp_extractor()
    result = fe.extract("")
    assert result.keywords == []
    assert result.entities == []


def test_hanlp_fallback_labels():
    """HanlpFeatureExtractor 降级模式：偏好词 → sentiment=positive。"""
    fe = _make_hanlp_extractor()
    result = fe.extract("用户偏好用 Python")
    assert result.labels.get("sentiment") == "positive"


def test_hanlp_fallback_negative_labels():
    """HanlpFeatureExtractor 降级模式：错误词 → sentiment=negative。"""
    fe = _make_hanlp_extractor()
    result = fe.extract("系统报错了，内存泄漏问题")
    assert result.labels.get("sentiment") == "negative"


def test_hanlp_fallback_language_labels():
    """HanlpFeatureExtractor 降级模式：中英文混合 → language=mixed。"""
    fe = _make_hanlp_extractor()
    result = fe.extract("用户偏好 Python 编程")
    assert result.labels.get("language") == "mixed"


def test_hanlp_no_fallback_health_fails():
    """HanlpFeatureExtractor 无降级：HanLP 不可用时 health() 抛异常。"""
    fe = _make_hanlp_extractor_no_fallback()
    if not HANLP_AVAILABLE:
        from common.errors import HealthCheckError

        try:
            fe.health()
        except HealthCheckError as exc:
            assert exc is not None, "health should raise HealthCheckError when unavailable"


def test_hanlp_no_fallback_extract_empty_result():
    """HanlpFeatureExtractor 无降级：HanLP 不可用时 extract() 返回空 FeatureSet。"""
    fe = _make_hanlp_extractor_no_fallback()
    if not HANLP_AVAILABLE:
        result = fe.extract("用户偏好 Python")
        assert result.keywords == []
        assert result.entities == []


def test_hanlp_factory_registration():
    """HanlpFeatureExtractor 通过工厂注册。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer

    assert "hanlp" in FeatureExtractorProducer.known()


# ---------------------------------------------------------------------------
# HanLP 可用时的深度测试
# ---------------------------------------------------------------------------


def test_hanlp_extract_with_model():
    """HanlpFeatureExtractor HanLP 可用时：完整 NER + POS 提取。"""
    if not HANLP_AVAILABLE:
        return

    fe = _make_hanlp_extractor()
    result = fe.extract("张三在北京的清华大学工作")
    assert isinstance(result, FeatureSet)
    assert len(result.keywords) > 0


def test_hanlp_health_ok_when_available():
    """HanlpFeatureExtractor HanLP 可用时：health() 返回 None。"""
    if not HANLP_AVAILABLE:
        return

    fe = _make_hanlp_extractor()
    assert fe.health() is None


# ---------------------------------------------------------------------------
# FeatureExtractorProducer 工厂测试
# ---------------------------------------------------------------------------


def test_producer_all_known():
    """FeatureExtractorProducer 已注册 keyword + spacy + hanlp。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer

    known = FeatureExtractorProducer.known()
    assert "keyword" in known
    assert "spacy" in known
    assert "hanlp" in known


def test_producer_create_keyword():
    """FeatureExtractorProducer.create("keyword") 返回 KeywordFeatureExtractor。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer
    from common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
        KeywordFeatureExtractor,
    )
    from config import AssemblyContext

    fe = FeatureExtractorProducer.build("keyword", {}, AssemblyContext())
    assert isinstance(fe, KeywordFeatureExtractor)


def test_producer_create_spacy():
    """FeatureExtractorProducer.create("spacy") 返回 SpacyFeatureExtractor。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer
    from common.feature_extractor.feature_extractor_impl.spacy_feature_extractor import (
        SpacyFeatureExtractor,
    )
    from config import AssemblyContext

    fe = FeatureExtractorProducer.build("spacy", {}, AssemblyContext())
    assert isinstance(fe, SpacyFeatureExtractor)


def test_producer_create_hanlp():
    """FeatureExtractorProducer.create("hanlp") 返回 HanlpFeatureExtractor。"""
    from common.feature_extractor.feature_extractor_impl import FeatureExtractorProducer
    from common.feature_extractor.feature_extractor_impl.hanlp_feature_extractor import (
        HanlpFeatureExtractor,
    )
    from config import AssemblyContext

    fe = FeatureExtractorProducer.build("hanlp", {}, AssemblyContext())
    assert isinstance(fe, HanlpFeatureExtractor)
