"""JiebaTokenizer 单元测试——真实 jieba 分词验证。

不使用 Mock——直接调用 jieba 分词库验证：
- 中文分词质量（搜索模式 vs 精确模式）
- 英文分词
- 中英混合分词
- 空输入
- HMM 开关
- 延迟初始化
- Producer 集成
"""

from __future__ import annotations

import pytest

from common.base import PluginType
from common.tokenizer.tokenizer_impl import TokenizerProducer
from common.tokenizer.tokenizer_impl.jieba_tokenizer import JiebaTokenizer

# ---------------------------------------------------------------------------
# Tests: Core interface
# ---------------------------------------------------------------------------


def test_plugin_type():
    """T-JB-01: plugin_type 返回 TOKENIZER。"""
    t = JiebaTokenizer()
    assert t.plugin_type() == PluginType.TOKENIZER


def test_health():
    """T-JB-02: health 正常时返回 None（也触发词典加载）。"""
    t = JiebaTokenizer()
    result = t.health()
    assert result is None


def test_tokenize_chinese_search_mode():
    """T-JB-03: 搜索引擎模式——中文分词，长词二次切分提高召回。"""
    t = JiebaTokenizer(mode="search")
    tokens = t.tokenize("用户偏好简洁回答风格")
    # search 模式应将"简洁"、"回答"、"风格"独立切出
    assert "简洁" in tokens
    assert "回答" in tokens
    assert "风格" in tokens
    assert "偏好" in tokens


def test_tokenize_chinese_default_mode():
    """T-JB-04: 精确模式——中文分词，不二次切分长词。"""
    t = JiebaTokenizer(mode="default")
    tokens = t.tokenize("人工智能深度学习模型训练")
    # 精确模式应产出有意义的词组
    assert len(tokens) > 1
    assert "人工智能" in tokens or "深度学习" in tokens


def test_tokenize_english():
    """T-JB-05: 英文文本分词。"""
    t = JiebaTokenizer(mode="search")
    tokens = t.tokenize("Python debugging standard workflow")
    assert "Python" in tokens
    assert "debugging" in tokens


def test_tokenize_mixed():
    """T-JB-06: 中英混合文本分词——中文用 jieba、英文按空格。"""
    t = JiebaTokenizer(mode="search")
    tokens = t.tokenize("用户偏好用Python写代码")
    assert "用户" in tokens
    assert "偏好" in tokens
    assert "Python" in tokens


def test_tokenize_empty():
    """T-JB-07: 空 string 返回空 list。"""
    t = JiebaTokenizer()
    assert t.tokenize("") == []


def test_tokenize_whitespace_only():
    """T-JB-08: 纯空白 string 返回空 list（strip 后无 token）。"""
    t = JiebaTokenizer()
    assert t.tokenize("   ") == []


def test_tokenize_batch():
    """T-JB-09: 批量分词，顺序与输入一致。"""
    texts = ["用户偏好简洁", "Python调试流程"]
    t = JiebaTokenizer(mode="search")
    results = t.tokenize_batch(texts)
    assert len(results) == 2
    # 单条结果与批量结果一致
    assert results[0] == t.tokenize("用户偏好简洁")
    assert results[1] == t.tokenize("Python调试流程")


def test_hmm_toggle():
    """T-JB-10: HMM=True 发现新词 vs HMM=False 不发现新词。"""
    t_hmm = JiebaTokenizer(mode="default", hmm=True)
    t_no_hmm = JiebaTokenizer(mode="default", hmm=False)
    # 对同一文本，HMM 产出的 token 数可能不同（新词发现差异）
    tokens_hmm = t_hmm.tokenize("深度学习神经网络反向传播算法")
    tokens_no_hmm = t_no_hmm.tokenize("深度学习神经网络反向传播算法")
    # 两者都应产出有意义的分词结果（具体差异取决于词典+HMM）
    assert len(tokens_hmm) > 1
    assert len(tokens_no_hmm) > 1


def test_lazy_initialization():
    """T-JB-11: 延迟初始化——构造时不加载词典，首次 tokenize 时加载。"""
    t = JiebaTokenizer()
    assert not t.initialized  # 构造时未初始化
    # 首次 tokenize 触发加载
    tokens = t.tokenize("测试")
    assert t.initialized
    assert len(tokens) > 0


def test_deterministic():
    """T-JB-12: 同一输入产出相同分词结果（确定性）。"""
    t = JiebaTokenizer(mode="search")
    text = "自然语言处理是人工智能的重要方向"
    tokens1 = t.tokenize(text)
    tokens2 = t.tokenize(text)
    assert tokens1 == tokens2


def test_search_vs_default_recall():
    """T-JB-13: 搜索模式应比精确模式产出更多 token（二次切分长词）。"""
    t_search = JiebaTokenizer(mode="search")
    t_default = JiebaTokenizer(mode="default")
    text = "搜索引擎优化技术"
    tokens_search = t_search.tokenize(text)
    tokens_default = t_default.tokenize(text)
    # 搜索模式产出的 token 数 >= 精确模式
    assert len(tokens_search) >= len(tokens_default)


# ---------------------------------------------------------------------------
# Tests: Producer / Config
# ---------------------------------------------------------------------------


def test_producer_known():
    """T-JB-P01: TokenizerProducer 已注册 jieba。"""
    assert "jieba" in TokenizerProducer.known()


def test_producer_create():
    """T-JB-P02: TokenizerProducer.create("jieba") 返回 JiebaTokenizer 实例。"""
    from config import AssemblyContext

    tokenizer = TokenizerProducer.build("jieba", {}, AssemblyContext())
    assert isinstance(tokenizer, JiebaTokenizer)
    # 默认 Config 应使用 search 模式 + HMM=True
    assert tokenizer.mode == "search"
    assert tokenizer.hmm is True


def test_producer_create_custom_config():
    """T-JB-P03: Config 自定义 jieba 参数。"""
    from config import AssemblyContext

    tokenizer = TokenizerProducer.build(
        "jieba",
        {"tokenizer_jieba_mode": "default", "tokenizer_jieba_hmm": False},
        AssemblyContext(),
    )
    assert isinstance(tokenizer, JiebaTokenizer)
    assert tokenizer.mode == "default"
    assert tokenizer.hmm is False


def test_producer_unknown_type():
    """T-JB-P04: 不支持的 tokenizer_type 抛 ValidationError。"""
    from common.errors import ValidationError
    from config import AssemblyContext

    with pytest.raises(ValidationError):
        TokenizerProducer.build("unknown", {}, AssemblyContext())


def test_assemble_with_jieba():
    """T-JB-P05: assemble(config) 使用 jieba tokenizer 全链路可运行。"""
    from api.memory_api_impl import assemble
    from common.type_def import Context, Modality, Scope
    from config import Config

    # 两级命名空间：覆盖 tokenizer 顶层的 default 实例为 jieba；embedder 沿用默认 hashing，
    # 其 tokenizer 依赖经 default 引用同样取到 jieba → 全链路 jieba。
    config = Config.from_dict({"tokenizer": {"default": "jieba"}})
    api = assemble(config=config)
    scope = Scope(org="test", user="alice", agent="a1", session="s1")
    actor = Scope(org="test", user="alice")
    # add → jieba 分词建索引 → search
    units = api.add("用户偏好简洁回答", scope, source=Modality.TEXT, identity=actor)
    assert len(units) == 1
    result = api.search("偏好", Context(scope), identity=actor, top_k=10)
    assert len(result.items) > 0
