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

# 单个 unit 进 prompt 的默认字符上限（约 2-3K token）。
DEFAULT_MAX_UNIT_CHARS = 4000


def truncate_unit_content(content: str, limit: int = DEFAULT_MAX_UNIT_CHARS) -> str:
    """超长 unit 内容截断为头 3/4 + 尾 1/4，中段以省略标记替代。

    头尾保留是因为主题/类型特征通常集中在开头与结尾；limit <= 0 视为不设防
    （仅测试/特殊场景使用）。未超限的输入原样返回，prompt 字节不变。
    """
    if limit <= 0 or len(content) <= limit:
        return content
    head = limit * 3 // 4
    tail = limit // 4
    omitted = len(content) - head - tail
    return f"{content[:head]}\n...[TRUNCATED {omitted} chars]...\n{content[-tail:]}"
