"""construction.common.parse_tags 单元测试。

精确覆盖 extractor/classifier 共享的 tags 解析口径，卡住三处历史不一致：
纯数字过滤、大小写不敏感去重、截断到 ≤MAX_TAGS。
"""

from construction.common import MAX_TAGS, parse_tags


def test_non_list_returns_empty():
    """非 list 输入容错返空。"""
    assert parse_tags(None) == []
    assert parse_tags("Python") == []
    assert parse_tags({"x": 1}) == []


def test_strips_and_drops_empty():
    """逐项 strip；空串/纯空白丢弃。"""
    assert parse_tags(["  Python  ", "", "   "]) == ["Python"]


def test_pure_digit_filtered():
    """纯数字 tag 丢弃（口径对齐 classifier，不再保留 "123"）。"""
    assert parse_tags(["123", "Python", "456"]) == ["Python"]


def test_case_insensitive_dedup_keeps_first_form():
    """大小写不敏感去重：保留首次出现的原始大小写形式。"""
    # "Python" 与 "python" 视为同一 tag，只留首次的 "Python"
    assert parse_tags(["Python", "python", "PYTHON"]) == ["Python"]
    # 首次若为小写则保留小写
    assert parse_tags(["python", "Python"]) == ["python"]


def test_capped_to_max_tags():
    """截断到 MAX_TAGS（保序，取前 N 个）。"""
    many = [f"t{i}" for i in range(MAX_TAGS + 5)]
    out = parse_tags(many)
    assert len(out) == MAX_TAGS
    assert out == [f"t{i}" for i in range(MAX_TAGS)]


def test_non_str_elements_coerced():
    """非 str 元素逐项 str() 化后清洗（数字被 isdigit 过滤，None 转成串 'None' 保留）。"""
    assert parse_tags([123, "Python", None]) == ["Python", "None"]
