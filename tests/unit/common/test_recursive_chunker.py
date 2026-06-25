"""RecursiveChunker 单元测试。

验证递归分块的核心逻辑：
- 按分隔符层级递归切分
- 碎片合并
- overlap 回溯
- 空文本处理
- start/end 偏移准确性
- 长文本多 chunk 分组
"""

from common.base import PluginType
from common.chunker.chunker_impl.recursive_chunker import RecursiveChunker

# ---------------------------------------------------------------------------
# 通用 helper
# ---------------------------------------------------------------------------


def _make_chunker(
    chunk_size: int = 200,
    overlap: int = 50,
    min_chunk: int = 10,
    separators=None,
) -> RecursiveChunker:
    return RecursiveChunker(
        chunk_size_chars=chunk_size,
        overlap_chars=overlap,
        min_chunk_chars=min_chunk,
        separators=separators,
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_plugin_type():
    """RecursiveChunker.plugin_type() 返回 CHUNKER。"""
    chunker = _make_chunker()
    assert chunker.plugin_type() == PluginType.CHUNKER


def test_health():
    """RecursiveChunker.health() 正常时不抛异常。"""
    chunker = _make_chunker()
    assert chunker.health() is None


def test_empty_text():
    """空文本 → 返回空列表。"""
    chunker = _make_chunker()
    assert chunker.chunk("", "u1") == []
    assert chunker.chunk("   ", "u1") == []


def test_short_text_single_chunk():
    """短文本（字符数 ≤ chunk_size）→ 返回单个 chunk。"""
    chunker = _make_chunker(chunk_size=200)
    text = "用户偏好 Python"
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].seq == 0
    assert chunks[0].unit_id == "u1"
    assert chunks[0].start == 0
    assert chunks[0].end == len(text)
    assert chunks[0].id == "0"


def test_paragraph_split():
    """双换行段落分隔 → 每个段落独立 chunk。"""
    chunker = _make_chunker(chunk_size=30)
    text = (
        "第一段内容关于Python编程语言和数据分析工具。\n\n"
        "第二段内容关于Java虚拟机和Spring框架。\n\n"
        "第三段内容关于Go语言的并发模型和goroutine。"
    )
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) >= 2
    all_text = "".join(c.text for c in chunks)
    assert "Python" in all_text
    assert "Java" in all_text
    assert "Go" in all_text


def test_sentence_split():
    """长段落 → 降级到句子分隔。"""
    chunker = _make_chunker(chunk_size=50, overlap=10)
    text = "".join(
        [
            "用户偏好Python编程语言。用户也喜欢Java虚拟机。",
            "用户不喜欢C加加语言。用户认为Go并发很好用。",
        ]
    )
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= chunker.chunk_size + 20


def test_overlap_between_chunks():
    """相邻 chunk 之间有 overlap（共享文本片段）。"""
    chunker = _make_chunker(chunk_size=50, overlap=20)
    text = (
        "句子A关于Python编程语言。句子B关于Java虚拟机和框架。"
        "句子C关于Go语言并发编程。句子D关于Rust内存安全。"
    )
    chunks = chunker.chunk(text, "u1")

    if len(chunks) >= 2:
        import re

        overlap_found = False
        for i in range(len(chunks) - 1):
            tail_words = set(re.findall(r"\w+", chunks[i].text))
            head_words = set(re.findall(r"\w+", chunks[i + 1].text))
            common = tail_words & head_words
            if common:
                overlap_found = True
        assert overlap_found, "相邻 chunk 之间应有 overlap"


def test_chunk_id_with_unit_id():
    """chunk id 为分块顺序编号（纯数字序号）。"""
    chunker = _make_chunker(chunk_size=30)
    text = "".join(
        [
            "这是一段足够长的文本，包含多个句子和段落。",
            "第一句关于Python。第二句关于Java。第三句关于Go。",
        ]
    )
    chunks = chunker.chunk(text, "unit42")

    for i, c in enumerate(chunks):
        assert c.id == str(i)
        assert c.unit_id == "unit42"


def test_chunk_id_without_unit_id():
    """chunk id 与 unit_id 无关，始终为纯数字序号。"""
    chunker = _make_chunker(chunk_size=30)
    text = "这是第一句关于Python。这是第二句关于Java。"
    chunks = chunker.chunk(text, "")

    for i, c in enumerate(chunks):
        assert c.id == str(i)


def test_start_end_offsets():
    """chunk 的 start/end 偏移指向原文中的正确位置。"""
    chunker = _make_chunker(chunk_size=200)
    text = "Hello world."
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) == 1
    assert chunks[0].start == 0
    assert chunks[0].end == len(text)


def test_start_end_offsets_multi_chunk():
    """多 chunk 时 start/end 偏移仍然准确。"""
    chunker = _make_chunker(chunk_size=50, overlap=10)
    text = "第一句话的内容。第二句话的内容。第三句话的内容。第四句话的内容。"
    chunks = chunker.chunk(text, "u1")

    for c in chunks:
        assert c.start >= 0
        assert c.end <= len(text)
        assert c.start < c.end


def test_metadata_passthrough():
    """metadata 从 chunk() 入参透传到每个 Chunk。"""
    chunker = _make_chunker(chunk_size=200)
    text = "Hello"
    meta = {"tier": "episodic", "scope": "test/alice"}
    chunks = chunker.chunk(text, "u1", metadata=meta)

    assert len(chunks) == 1
    assert chunks[0].metadata == meta


def test_seq_numbering():
    """chunk 的 seq 从 0 开始递增。"""
    chunker = _make_chunker(chunk_size=50, overlap=10)
    text = "".join(
        [
            "句子一关于Python编程。句子二关于Java虚拟机。",
            "句子三关于Go并发。句子四关于Rust安全。",
        ]
    )
    chunks = chunker.chunk(text, "u1")

    for i, c in enumerate(chunks):
        assert c.seq == i


def test_custom_separators():
    """自定义分隔符层级：通过构造函数传入。"""
    chunker = _make_chunker(chunk_size=20, overlap=5, min_chunk=1, separators=[[" "]])
    text = "word1 word2 word3 word4 word5 word6 word7 word8"
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count > 0


def test_no_separator_fallback():
    """文本中无任何分隔符 → 返回单个 chunk（整段）。"""
    chunker = _make_chunker(chunk_size=200)
    text = "这是一段没有任何分隔符的连续中文文本"
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) == 1
    assert chunks[0].text == text


def test_mixed_chinese_english():
    """中英文混合文本正确切分。"""
    chunker = _make_chunker(chunk_size=40, overlap=10)
    text = (
        "用户偏好Python进行数据分析，经常使用NumPy和Pandas库。\n\n"
        "Agent喜欢用Java写脚本，Spring框架很好用。\n\n"
        "系统报错内存泄漏，需要排查问题。"
    )
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) >= 2
    all_text = "".join(c.text for c in chunks)
    assert "Python" in all_text
    assert "Java" in all_text


def test_deep_recursion():
    """深层递归：一段超长 → 句子 → 短语 → 空格逐级降级。"""
    chunker = _make_chunker(chunk_size=30, overlap=5, min_chunk=5)
    text = (
        "This is a very long paragraph with multiple sentences, each containing several words, "
        "and some commas too. More text here. And even more."
    )
    chunks = chunker.chunk(text, "u1")

    assert len(chunks) >= 3
    for c in chunks:
        assert c.token_count <= chunker.chunk_size + 20


def test_deterministic_output():
    """相同输入产出相同结果（幂等性）。"""
    chunker = _make_chunker(chunk_size=60, overlap=15)
    text = "".join(
        [
            "第一句话关于Python。第二句话关于Java。第三句话关于Go。",
            "第四句话关于Rust。第五句话关于C。",
        ]
    )

    result1 = chunker.chunk(text, "u1")
    result2 = chunker.chunk(text, "u1")

    assert len(result1) == len(result2)
    for c1, c2 in zip(result1, result2):
        assert c1.text == c2.text
        assert c1.start == c2.start
        assert c1.end == c2.end
        assert c1.seq == c2.seq
