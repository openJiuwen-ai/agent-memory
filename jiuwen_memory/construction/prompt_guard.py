# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LLM prompt 输入护栏：把 unit.content 拼进 prompt 的调用点必须经过本模块。

分类/抽取/关联/路由等判定类任务只依赖主题与结构特征，不需要全文；超长输入
既不提升判定质量，又会在同步 HTTP 路径上造成分钟级阻塞——单条 500KB 内容
全文单发 LLM 时，出站超时（默认 300s）× 重试次数足以把一次 add 拖到 900s+
无响应，且重试对确定性超时毫无意义。

护栏只截断 **判定输入**，不动存储与索引（embed / 全文索引仍使用全文），
因此不影响检索质量；配套的 ingest 预切分（SimpleIngestor.max_unit_chars）
负责把超大文本拆成多个 unit 以保留完整信息。
"""

from __future__ import annotations

from dataclasses import replace

from jiuwen_memory.common.security.privacy.base import (
    protected_text_boundary,
    split_protected_text,
)
from jiuwen_memory.common.text_split import split_within_limit
from jiuwen_memory.common.type_def import MemoryUnit, Segment

# 判定类任务（分类/路由/关联/分层标注）只依赖主题与结构特征，不需要全文。
DEFAULT_MAX_UNIT_CHARS = 4000

# 内容型任务（抽取/抽象/分层摘要）的事实分布覆盖全篇，中段截断等于静默漏抽——
# 上限必须 ≥ ingest 预切分上限（SimpleIngestor 默认 8000），预切分产物才能完整
# 进入内容型 prompt；两处阈值改任一处须同步（见 construction/AGENTS.md 约束）。
# 超限内容不做截断：经 chunk_unit/chunk_unit_content 无损分批、逐批调用后合并，
# 兼顾自定义 Ingestor / 关闭预切分等路径的合法超限输入（部分失败必须显式）。
CONTENT_MAX_UNIT_CHARS = 8000


def truncate_unit_content(content: str, limit: int = DEFAULT_MAX_UNIT_CHARS) -> str:
    """超长 unit 内容截断为头 3/4 + 尾 1/4，中段以省略标记替代。

    头尾保留是因为主题/类型特征通常集中在开头与结尾；limit <= 0 视为不设防
    （仅测试/特殊场景使用）。未超限的输入原样返回，prompt 字节不变。
    """
    if limit <= 0 or len(content) <= limit:
        return content
    head = limit * 3 // 4
    tail = limit // 4
    head = protected_text_boundary(content, head)
    tail_start = protected_text_boundary(content, len(content) - tail, right=True)
    omitted = tail_start - head
    return f"{content[:head]}\n...[TRUNCATED {omitted} chars]...\n{content[tail_start:]}"


def chunk_unit_content(
    content: str, limit: int = CONTENT_MAX_UNIT_CHARS
) -> list[str]:
    """内容型任务的无损分批：未超限单批；超限按段落聚合切为 ≤ limit 的批次。

    与 ingest 预切分共用同一份算法（common.text_split.split_within_limit），
    join 严格还原原文——分批消费不丢任何内容，单批 prompt 仍有界。
    """
    return split_within_limit(content, limit)


def chunk_unit(unit: MemoryUnit, limit: int = CONTENT_MAX_UNIT_CHARS) -> list[MemoryUnit]:
    """unit 级无损分批：未超限返回 [unit] 原对象；超限返回内容分片副本。

    副本保留 id/scope/metadata 等全部字段，仅把 segments 换成单段分片——
    候选经 ``source_id``/``source_unit_ids`` 回指时仍指向原 unit。
    """
    chunks = (
        split_protected_text(unit.content, limit)
        if "privacy_level" in unit.system_metadata
        else chunk_unit_content(unit.content, limit)
    )
    if len(chunks) == 1:
        return [unit]
    return [
        replace(
            unit,
            segments=[
                Segment(
                    content=chunk,
                    assets=list(unit.assets),
                    source=unit.source,
                )
            ],
        )
        for chunk in chunks
    ]
