"""最小实现：:class:`~construction.abstractor.Abstractor`。

把多条记忆概括成一条**高抽象粒度**的 CORE「画像」单元：内容为各来源内容的
拼接摘要，``provenance`` 记全部来源 id、打 ``profile`` 标签。真实实现会用 LLM
做真正的概括/升华，这里用拼接作可复现占位；少于 2 条不产出。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    MemoryTier,
    MemoryUnit,
    Segment,
    Temporal,
    inherited_system_metadata,
    inherited_user_metadata,
)
from jiuwen_memory.construction.abstractor import Abstractor, AbstractorProducer
from jiuwen_memory.construction.base import OperatorType

logger = get_logger(__name__)


class ConcatAbstractor(Abstractor):
    """把多条记忆拼接概括为一条 CORE 画像单元（高抽象，记全量血缘）。"""

    def operator_type(self) -> OperatorType:
        """返回当前算子类型。

        Returns:
            返回 OperatorType。
        """
        return OperatorType.ABSTRACTOR

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def abstract(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """执行 `abstract` 操作。

        Args:
            units: 参数 units（List[MemoryUnit]）。

        Returns:
            返回 List[MemoryUnit]。
        """
        sources = [u for u in units if u.lifecycle.value == "active"]
        logger.info(
            "ConcatAbstractor: received %d units, %d active sources", len(units), len(sources)
        )
        for u in units:
            logger.info(
                "ConcatAbstractor: input unit id=%s lifecycle=%s tier=%s provenance=%s content=%s",
                u.id[:8],
                u.lifecycle.value,
                u.tier.value,
                u.provenance,
                u.content[:200],
            )
        if len(sources) < 2:
            logger.info("ConcatAbstractor: fewer than 2 active sources, no output")
            return []
        now = datetime.now(timezone.utc)
        summary = "；".join(u.content for u in sources)
        result = [
            MemoryUnit(
                id=str(uuid.uuid4()),
                scope=sources[0].scope,
                tier=MemoryTier.CORE,
                segments=[Segment(content=f"画像综合（{len(sources)} 条）：{summary}")],
                provenance=[u.id for u in sources],
                tags=["profile"],
                system_metadata=inherited_system_metadata(sources),
                user_metadata=inherited_user_metadata(sources),
                temporal=Temporal(
                    t_event=now,
                    t_ingest=now,
                    t_valid=now,
                    t_message=sources[0].temporal.t_message,
                ),
            )
        ]
        for r in result:
            logger.info(
                "ConcatAbstractor: output unit id=%s tier=%s provenance=%s content=%s",
                r.id[:8],
                r.tier.value,
                r.provenance,
                r.content[:200],
            )
        return result


# -- 注册到 AbstractorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@AbstractorProducer.register("concat")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return ConcatAbstractor()
