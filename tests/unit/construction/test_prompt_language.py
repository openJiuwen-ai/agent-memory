"""Guard the English construction prompts against mixed-language examples."""

import re

from jiuwen_memory.construction.classifier_impl.llm_classifier import _CLASSIFY_SYSTEM_PROMPT
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    _EXTRACT_SYSTEM_PROMPT,
    _PROCEDURAL_SYSTEM_PROMPT,
)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def test_english_prompts_do_not_contain_chinese_examples():
    assert _CJK.search(_PROCEDURAL_SYSTEM_PROMPT) is None
    assert _CJK.search(_CLASSIFY_SYSTEM_PROMPT) is None
    assert _CJK.search(_EXTRACT_SYSTEM_PROMPT) is None
