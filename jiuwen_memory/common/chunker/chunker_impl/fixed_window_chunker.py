"""最小实现：:class:`~common.chunker.base.Chunker`——定长字符窗口切分。

按固定字符窗口（默认 120，无重叠）把内容切成有序 :class:`~common.type_def.Chunk`，
每块带 ``unit_id``、序号、起止偏移与透传 metadata。规则确定 → 重切结果可复现
（重索引/演进路径据此重建派生索引）。
真实实现会按句子/语义边界切，这里用定长占位。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Chunk

logger = get_logger(__name__)


class FixedWindowChunker(Chunker):
    """定长字符窗口切分器。"""

    def __init__(self, size: int = 120) -> None:
        """初始化 FixedWindowChunker。

        Args:
            size: 参数 size（int）。
        """
        self._size = size

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.CHUNKER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def chunk(
        self, text: str, unit_id: str = "", metadata: Optional[Dict[str, str]] = None
    ) -> List[Chunk]:
        """将输入文本切分为多个片段。

        Args:
            text: 参数 text（str）。
            unit_id: 参数 unit_id（str）。
            metadata: 参数 metadata（Optional[Dict[str, str]]）。

        Returns:
            返回 List[Chunk]。
        """
        meta = dict(metadata or {})
        chunks: List[Chunk] = []
        for seq, start in enumerate(range(0, max(len(text), 1), self._size)):
            end = start + self._size
            piece = text[start:end]
            chunks.append(
                Chunk(
                    id=str(seq),
                    unit_id=unit_id,
                    seq=seq,
                    text=piece,
                    start=start,
                    end=start + len(piece),
                    token_count=len(piece),
                    metadata=meta,
                )
            )
        logger.info(
            "FixedWindowChunker: chunked %d chars into %d pieces (size=%d)",
            len(text),
            len(chunks),
            self._size,
        )
        return chunks


# -- 注册到 ChunkerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@ChunkerProducer.register("fixed_window")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return FixedWindowChunker(size=config.get("chunk_size", 120))
