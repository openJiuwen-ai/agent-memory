"""RecursiveChunker — 递归分块实现。

按多层分隔符优先级递归切分：先尝试最高级分隔符（段落/换行），
若某段仍超过 chunk_size，则降级到下一级分隔符（句子），
再降级到词级，最终硬截断。每次切分在相邻 chunk 间保留 overlap
保证上下文连续性。

适用场景：向量索引构建——Embedding 模型有固定上下文窗，
超出时需切片才能正确编码。递归切分尽量保持语义边界完整，
避免在句中/词中截断。

与 keyword 索引（BM25）不同——关键词索引以整篇 content 为一篇
Document，不做切片，因为 TF-IDF 统计建立在整篇文档上。

切分判断基于字符长度（chunk_size_chars），不依赖 tokenizer。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Chunk

logger = get_logger(__name__)


# 默认分隔符层级：从大到小（段落 → 句子 → 短语 → 词）
_DEFAULT_SEPARATORS: list[list[str]] = [
    # 层级 0：段落分隔（双换行、章节标题）
    ["\n\n", "\r\n\r\n"],
    # 层级 1：句子分隔（单换行、句号、问号、感叹号）
    ["\n", "\r\n", "。", ".", "！", "!", "？", "?", "；", ";"],
    # 层级 2：短语分隔（逗号、冒号、括号）
    ["，", ",", "：", ":", "、", "（", "(", "）", ")"],
    # 层级 3：空格（英文词间）
    [" "],
]


@dataclass
class _Split:
    """递归切分产出的单个片段。"""

    text: str
    start: int  # 在原文中的起始字符偏移
    end: int  # 在原文中的结束字符偏移


class RecursiveChunker(Chunker):
    """递归分块：按分隔符优先级层级递归切分，保证语义边界完整。

    算法：
    1. 用最高级分隔符（如双换行）将文本切成段
    2. 若某段字符长度 > chunk_size，降级到下一级分隔符再切
    3. 递归直到所有片段 ≤ chunk_size，或到达最底层
    4. 合并过小碎片，按 chunk_size 分组
    5. 相邻 chunk 之间保留 overlap_chars 的重叠
    """

    def __init__(
        self,
        chunk_size_chars: int = 512,
        overlap_chars: int = 64,
        separators: list[list[str]] | None = None,
        min_chunk_chars: int = 10,
    ) -> None:
        """
        Args:
            chunk_size_chars: 每个 chunk 的最大字符数
            overlap_chars: 相邻 chunk 之间的重叠字符数
            separators: 分隔符层级列表，从大到小；默认使用中英文混合层级
            min_chunk_chars: 最小 chunk 字符数，低于此的碎片合并到前一个 chunk
        """
        self._chunk_size = chunk_size_chars
        self._overlap = overlap_chars
        self._separators = separators or _DEFAULT_SEPARATORS
        self._min_chunk = min_chunk_chars

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @staticmethod
    def _pick_separator(text: str, candidates: list[str]) -> str | None:
        """从候选分隔符中选取文本中出现的第一个。"""
        for sep in candidates:
            if sep in text:
                return sep
        return None

    @staticmethod
    def _split_keep_separator(text: str, sep: str) -> list[tuple[str, int, int]]:
        """按分隔符切分，分隔符保留在片段尾部，返回 (片段, 起始偏移, 结束偏移)。"""
        # text.split(sep) 会移除分隔符，需重新拼接
        parts = text.split(sep)

        result: list[tuple[str, int, int]] = []
        char_offset = 0

        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                # 非末段：拼接分隔符
                fragment = part + sep
            else:
                # 末段：原文末尾可能无分隔符
                fragment = part

            if fragment.strip():
                start = char_offset
                end = char_offset + len(fragment)
                result.append((fragment, start, end))

            char_offset += len(fragment)

        return result

    def plugin_type(self) -> PluginType:
        return PluginType.CHUNKER

    def health(self) -> None:
        return None

    def chunk(
        self,
        text: str,
        unit_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """将 text 递归切分为有序 chunk。"""
        if not text.strip():
            return []

        metadata = metadata or {}
        logger.info(
            "RecursiveChunker: chunking %d chars (chunk_size=%d, overlap=%d)",
            len(text),
            self._chunk_size,
            self._overlap,
        )

        # Step 1: 递归切分 → 原子片段列表
        splits = self._recursive_split(text, self._separators)

        # Step 2: 合并过小碎片
        merged = self._merge_small(splits)

        # Step 3: 按 chunk_size 分组 + overlap → 最终 Chunk
        chunks = self._build_chunks(merged, unit_id, metadata)
        logger.info("RecursiveChunker: produced %d chunks", len(chunks))
        return chunks

    # ------------------------------------------------------------------
    # 递归切分核心
    # ------------------------------------------------------------------

    def _recursive_split(
        self,
        text: str,
        separator_levels: list[list[str]],
        level: int = 0,
    ) -> list[_Split]:
        """按分隔符层级递归切分，返回 _Split 片段列表。

        偏移量始终相对于原始输入 text，通过逐层累加修正。
        """
        if level >= len(separator_levels):
            # 最底层：无法再按分隔符切，直接返回整段
            return [_Split(text=text, start=0, end=len(text))]

        # 找到当前层级中第一个出现在文本里的分隔符
        separators = separator_levels[level]
        primary_sep = self._pick_separator(text, separators)
        if primary_sep is None:
            # 当前层级无此分隔符，直接降级
            return self._recursive_split(text, separator_levels, level + 1)

        # 按该分隔符切分（分隔符保留在片段尾部）
        fragments = self._split_keep_separator(text, primary_sep)

        # 对每个片段判断是否需要继续降级
        result: list[_Split] = []
        for frag_text, frag_start, frag_end in fragments:
            frag_len = len(frag_text)

            if frag_len > self._chunk_size:
                # 超长：降级继续切分，偏移修正
                sub_splits = self._recursive_split(frag_text, separator_levels, level + 1)
                for s in sub_splits:
                    result.append(
                        _Split(text=s.text, start=frag_start + s.start, end=frag_start + s.end)
                    )
            else:
                result.append(_Split(text=frag_text, start=frag_start, end=frag_end))

        return result

    # ------------------------------------------------------------------
    # 合并过小碎片
    # ------------------------------------------------------------------

    def _merge_small(self, splits: list[_Split]) -> list[_Split]:
        """将字符数 < min_chunk 的碎片合并到前一个片段。"""
        if not splits:
            return splits

        merged: list[_Split] = [splits[0]]

        for s in splits[1:]:
            if len(s.text) < self._min_chunk and merged:
                # 合并到前一个
                prev = merged[-1]
                merged[-1] = _Split(text=prev.text + s.text, start=prev.start, end=s.end)
            else:
                merged.append(s)

        return merged

    # ------------------------------------------------------------------
    # 分组 + overlap → Chunk
    # ------------------------------------------------------------------

    def _build_chunks(
        self,
        splits: list[_Split],
        unit_id: str,
        metadata: dict[str, Any],
    ) -> list[Chunk]:
        """将 splits 按 chunk_size 分组加 overlap，产出 Chunk 列表。

        分组策略：
        - 累积 splits 直到总字符数 > chunk_size → 封装为一组
        - 封装前从尾部回溯 overlap_chars，这些回溯片段同时作为下一组开头
        - 确保最后一组也被封装
        """
        if not splits:
            return []

        # 总字符数 ≤ chunk_size → 单 chunk 直接返回
        total = sum(len(s.text) for s in splits)
        if total <= self._chunk_size:
            full_text = "".join(s.text for s in splits)
            return [
                Chunk(
                    id="0",
                    unit_id=unit_id,
                    seq=0,
                    text=full_text,
                    start=splits[0].start,
                    end=splits[-1].end,
                    token_count=len(full_text),
                    metadata=metadata,
                )
            ]

        # 多组分组
        groups: list[list[_Split]] = []
        current: list[_Split] = []
        current_len = 0

        for s in splits:
            s_len = len(s.text)

            # 加入当前片段会超出 chunk_size → 封装当前组
            if current_len + s_len > self._chunk_size and current:
                groups.append(current)

                # overlap 回溯：从当前组尾部取 overlap_chars 的片段作为下一组开头
                overlap_splits: list[_Split] = []
                overlap_chars = 0
                for prev_s in reversed(current):
                    prev_len = len(prev_s.text)
                    overlap_splits.insert(0, prev_s)
                    overlap_chars += prev_len
                    if overlap_chars >= self._overlap:
                        break

                # 下一组以 overlap 片段开头
                current = overlap_splits
                current_len = overlap_chars

            # 加入当前片段
            current.append(s)
            current_len += s_len

        # 最后一组
        if current:
            groups.append(current)

        # 构建 Chunk 对象
        chunks: list[Chunk] = []
        for seq, group in enumerate(groups):
            text = "".join(s.text for s in group)
            chunks.append(
                Chunk(
                    id=str(seq),
                    unit_id=unit_id,
                    seq=seq,
                    text=text,
                    start=group[0].start,
                    end=group[-1].end,
                    token_count=len(text),
                    metadata=metadata,
                )
            )

        return chunks


# -- 注册到 ChunkerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@ChunkerProducer.register("recursive")
def _build(config):
    size = config.get("chunk_size", 512)
    return RecursiveChunker(
        chunk_size_chars=size,
        overlap_chars=size // 8 if size else 64,
        min_chunk_chars=10,
    )
