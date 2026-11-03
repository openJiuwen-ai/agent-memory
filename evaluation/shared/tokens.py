"""评测侧 token 计数：上下文打包（F09）的预算与降幅统一按 cl100k 计。"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def cl100k_tokens(text: str) -> int:
    """cl100k 分词数；LoCoMo 记忆块上按它与按 GLM-5.2 网关计得的降幅相差 < 0.3 个百分点。"""
    return len(_encoding().encode(text))
