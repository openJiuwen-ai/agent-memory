"""Guard the English construction prompts against mixed-language examples."""

import re

from construction.classifier_impl.llm_classifier import _CLASSIFY_SYSTEM_PROMPT
from construction.extractor_impl.llm_extractor import (
    _EXTRACT_SYSTEM_PROMPT,
    _PROCEDURAL_SYSTEM_PROMPT,
)


def test_english_prompts_do_not_contain_chinese_examples():
    prompts = _EXTRACT_SYSTEM_PROMPT + _PROCEDURAL_SYSTEM_PROMPT + _CLASSIFY_SYSTEM_PROMPT
    assert re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", prompts) is None
