"""construction 模块内的公共实现。

放抽取/分类等算子共享的小工具。此前 ``llm_extractor._parse_tags`` 与
``llm_classifier._parse_tags`` 两处独立维护，语义漂移出三处不一致（纯数字过滤、
去重 key、seen 语义）。集中到本模块，保证两条 LLM 路径对同一 ``tags`` 输入产出
同一结果。

``merge_unit_tags`` 负责把调用方 write tags 与 LLM/系统标记合并进派生 unit，
不对最终列表套 :data:`MAX_TAGS`（``MAX_TAGS`` 只约束 LLM 产出段）。
"""

from __future__ import annotations

# LLM 抽的 tags 上限（prompt 要求 1-3 个，解析端兜底截断）。
MAX_TAGS = 3


def parse_tags(raw) -> list[str]:
    """解析 LLM 输出的 tags：清洗（strip/去空/去纯数字/大小写不敏感去重）+ 截断到 ≤3。

    非法输入（非 list、元素非 str）容错：逐项 str() 化后 strip；空串/纯空白/纯数字丢弃；
    保留首次出现的（按 ``s.lower()`` 去重、保序）；最多取前 :data:`MAX_TAGS` 个。
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s or s.isdigit():
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(s)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def merge_unit_tags(*tag_lists: list[str]) -> list[str]:
    """多路 tags 合并：保序、大小写不敏感去重；不对最终结果套 :data:`MAX_TAGS`。

    典型顺序：write tags（调用方）→ LLM/主题 tags → 系统标记（``extracted`` /
    ``procedural``）。空串与纯空白丢弃；同 key 保留首次出现的原样写法。
    """
    seen: set[str] = set()
    out: list[str] = []
    for tags in tag_lists:
        for item in tags:
            s = str(item).strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out
