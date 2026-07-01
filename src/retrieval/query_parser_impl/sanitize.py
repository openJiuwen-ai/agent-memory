"""查询文本去噪：剥除上游包装元数据，保留用户真实检索意图。"""

from __future__ import annotations

import re

_BRACKET_UTC_RE = re.compile(r"\[[^\[\]]*UTC\]\s*")
_SENDER_LINE_RE = re.compile(
    r"(?im)^[^\S\r\n]*Sender[^\S\r\n]*\(untrusted metadata\)[^\S\r\n]*:[^\r\n]*(?:\r?\n|$)"
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_query(text: str | None, *, strip_code_fences: bool = False) -> str:
    """清洗单条检索 query，失败边界为返回空串而不是抛错。

    默认保留代码围栏，并保护围栏内部换行和缩进；只折叠围栏外空白。调用方若明确
    不希望代码参与检索，可通过 ``strip_code_fences=True`` 剥除整段围栏代码。
    """

    if not text:
        return ""

    current = str(text)
    if strip_code_fences:
        current = _CODE_FENCE_RE.sub(" ", current)
        return _normalize_outside_code(current)

    code_blocks: list[str] = []

    def protect(match: re.Match[str]) -> str:
        code_blocks.append(match.group(0))
        return f"<<<CODE_FENCE_{len(code_blocks) - 1}>>>"

    current = _CODE_FENCE_RE.sub(protect, current)
    current = _normalize_outside_code(current)

    for index, block in enumerate(code_blocks):
        current = current.replace(f"<<<CODE_FENCE_{index}>>>", block)
    return current.strip()


def _normalize_outside_code(text: str) -> str:
    """对非代码围栏内容做保守规整：去元数据、控制符与冗余空白。"""

    text = _BRACKET_UTC_RE.sub("", text)
    text = _SENDER_LINE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()
