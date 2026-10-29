# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""md 文件块解析共享逻辑：看门狗（``LocalWatchdog``）与 DomainStore（``edit_md_file``）
按同一口径从 md 文件读出 ``[(content_hash, content, block_index)]``。

口径对齐（F08 §D4 / F07 §12.3-12.4）：

- md 块结构是 ``# {标题}\\n{正文}\\n\\n``（``LocalMarkdownStore._render_block``），单段 unit
  约束下一行正文 = 一个块 = 一个 content_hash 候选。
- 按块结构解析（块间空行切块、每块首行为标题、其余为正文），而非逐行遍历——``#`` 开头
  的标题行与 ``#`` 开头的正文行（如 ``#1 里程碑``）在文本上不可区分，只能靠块结构区分
  （标题是块首行，正文在块内首行之后）。正文行不判 ``#`` 前缀，算 sha256。标题内容对
  diff / hash 无影响（标题行在任何读回路径中不被解析，见 ``_render_block`` 注释）。
- content_hash 算法与影子索引 ``SqliteDocumentShadowIndex._content_hash`` 同口径
  （``sqlite_shadow_index.py``）：``sha256(content.encode("utf-8")).hexdigest()``，不归一化。
  read_md_file / edit_md_file 侧算 hash 必须用本口径，否则与 shadow 落库的 content_hash
  对不上、``find_unit_ids_by_content_hash`` 反查落空。

本模块是「md 文件 → (content_hash, content, block_index)」的唯一定义点，看门狗与
``CompositeDomainStore._supersede_shadow_for_md`` 共用，避免两处各自抄一份按块解析逻辑
产生口径分叉。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any


def content_hash(content: str) -> str:
    """content 整段 sha256，不归一化。

    与 ``SqliteDocumentShadowIndex._content_hash``（``sqlite_shadow_index.py``）同口径——
    影子索引落库时对 ``segments[0].content`` 算此 hash 存进 ``memory_unit.content_hash`` 列，
    本函数算出的 hash 必须与之逐字节相等，``find_unit_ids_by_content_hash`` 才能命中。
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def collect_blocks_from_text(text: str) -> list[tuple[str, str, int]]:
    """从文本按块结构解析，返回 ``(content_hash, content, block_index)`` 列表。

    与 :func:`collect_md_blocks` 同口径（按空行切块、每块首行标题跳过、正文行算
    sha256、保序去重、带 block_index），区别仅在接收 ``str`` 而非 ``abs_path``——
    ``read_md_file``（``read_content`` 返回切片 ``str``）与 ``edit_md_file``（edit
    前后读全文 ``str``）拿到文本而非路径，共用本函数按块解析，不重复读文件。

    标题行与 ``#`` 开头的正文行不靠 ``#`` 前缀区分（那会把 ``#1 里程碑`` 等合法
    正文误当标题跳过），而是靠块结构区分：块首行是标题（跳过），块内首行之后的行
    是正文（不判 ``#`` 前缀）。无标题的裸正文块（用户手改直接加一行正文、块首非
    ``#``）整体视作正文；``#`` 开头的孤立单行（无正文）仍按标题跳过（与旧逐行
    口径一致——文本上无法区分「孤立标题」与「# 开头裸正文」）。
    """
    result: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    block: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if block:
                _append_block(block, result, seen)
                block = []
            continue
        block.append(line)
    if block:
        _append_block(block, result, seen)
    return result


def _append_block(
    block: list[str],
    result: list[tuple[str, str, int]],
    seen: set[str],
) -> None:
    """把一个块内的正文行（跳过块首标题行）逐行算 hash 追加进 result。

    block 首行若是 ``#`` 开头则为标题行，取 ``block[1:]`` 作正文；否则整个 block
    都是正文（裸正文块）。正文行不判 ``#`` 前缀，与 :func:`collect_blocks_from_text`
    的块结构口径一致。
    """
    body = block[1:] if block[0].startswith("#") else block
    for line in body:
        ch = content_hash(line)
        if ch in seen:
            continue
        seen.add(ch)
        result.append((ch, line, len(result)))


def collect_md_blocks(abs_path: str) -> list[tuple[str, str, int]]:
    """读 md 文件按块结构解析，返回 ``(content_hash, content, block_index)`` 列表。

    与 :func:`collect_blocks_from_text` 同口径（按空行切块、跳过块首标题行、正文行算
    sha256、保序去重），区别仅在多带 ``block_index``（正文行从 0 起的序号）——供
    ``_supersede_shadow_for_md`` 做跨块对齐（D4.1：替换前后块数一致时按 block_index 一一
    继承对应旧 unit 的元数据）。

    保序、去重（同一 content_hash 只保留首条，后续重复正文行跳过），避免同文件内重复
    正文行被当作多条新块重复建 unit。

    文件不存在视为「无正文块」返回空列表——调用方（看门狗 deleted=True / DomainStore
    容错）按空列表 diff 出「全删」语义，与看门狗 ``deleted=True`` 分支一致。需要区分
    「文件不存在」与「文件为空」的调用方自行先做 ``os.path.isfile`` 判断；本函数对不存在
    文件返回空列表（容错口径，与看门狗 ``os.path.isfile`` 守卫一致），不抛 NotFoundError——
    避免 edit_md_file 在「首次创建后被看门狗抢先同步」的竞态下误抛错。

    :param abs_path: md 文件绝对路径（调用方已 ``_safe_abs_path`` 校验不逃逸 root）。
    """
    if not os.path.isfile(abs_path):
        return []
    with open(abs_path, encoding="utf-8") as fh:
        text = fh.read()
    return collect_blocks_from_text(text)


def build_block_units(  # noqa: ARG001  — 保留 kwargs 供未来扩展，当前签名稳定即可
    md_filename: str,
    blocks: list[tuple[str, str, int]],
    scope: Any,
    **kwargs: Any,
) -> list:
    """占位：块 → MemoryUnit 的构造留给 DomainStore（依赖 ``MemoryUnit`` 等类型_def 导入）。

    本函数仅为未来把看门狗 ``_build_unit`` 也收进共享模块预留入口；当前
    ``_supersede_shadow_for_md`` 的「继承式 SUPERSEDE」新 unit 构造需要继承旧 unit 的完整
    元数据（``deepcopy`` 旧 unit + 换 segments + 新 id + supersedes），与看门狗 ``_build_unit``
    的「从路径反推缺省元数据」是两套不同构造逻辑，暂不共用。Step 1 只提取
    ``content_hash`` / ``collect_md_blocks`` 这两个无类型_def 依赖的纯函数。
    """
    raise NotImplementedError(
        "build_block_units 留待后续提取；当前看门狗 _build_unit 与 edit_md_file 的"
        "继承式构造是两套逻辑，未合并到共享模块。"
    )
