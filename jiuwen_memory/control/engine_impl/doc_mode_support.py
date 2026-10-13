# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""文档模式下 Engine 共用的真源适配辅助。

文档模式（``write_document=true``）下真源是 markdown + 影子索引，KV 不再持有
MemoryUnit。Engine 读取经 ``DomainStore`` 分流到影子索引，lifecycle 改写
（supersede/transition）在 Engine 侧绕过 KV LifecycleManager 直接落影子索引
（KVLifecycleManager 的 supersede/transition/sweep 都从 KVStore 取 unit，文档模式
下走它必 NotFoundError / 静默失效，见 doc-mode-supersede-kv-lifecycle-break）。
本模块收 CloudEngine / AgentEngine 两类 Engine 共用的纯函数，避免复制分叉。
"""

from __future__ import annotations

from jiuwen_memory.common.type_def import COORDS_KEY, MD_FILENAME_KEY, Context, MemoryUnit


def project_from_context(context: Context) -> str:
    """从 ``context.extensions["coords"]["project"]`` 取 project 标量，空落空串（F08）。

    ``read_md_file`` / ``edit_md_file`` 的 project 解析落点：Engine 层从 Context 拆 project
    标量显式传给 DomainStore（Context 不下沉存储层）。与 ``LocalMarkdownStore._project_of``
    的 coords 兜底口径一致——``coords["project"]`` 缺失或非 str 落空串（跨项目可见）。

    口径与 ``search`` 同派别：``context.extensions["coords"]`` 在 search 是窄化谓词、
    在 read/edit 是路径解析依据，都不参与鉴权（鉴权轴是 ``context.scope``）。
    """
    coords = context.extensions.get("coords")
    if not isinstance(coords, dict):
        return ""
    project = coords.get("project")
    if isinstance(project, str):
        return project.strip()
    return ""


def restore_coords_from_md_filename(new: MemoryUnit, old: MemoryUnit) -> None:
    """文档模式 supersede：从 old 的 ``md_filename``（落盘键，dumps 不剥除）反推
    project，回写进 ``new.system_metadata[COORDS_KEY]``。

    背景：old 经 ``shadow.get_units`` 读回，``coords`` 是 TRANSIENT 键（dumps 剥除），
    读回的 old 无 coords → deepcopy 的 new 也无 → ``domain_store.add`` 内
    ``_md_path._project_of`` 读 coords 落空 → md_filename 重算成 ``memory/default/``，
    新版与旧版不同文件、project 维丢失。md_filename 形如 ``memory/{project}/MEMORY.md``
    或 ``memory/{project}/daily_memory/YYYY-MM-DD.md``（user_memory 是 ``memory/USER.md``
    无 project 段，不回写）。已在 new.system_metadata 有 coords 时不覆盖。
    """
    meta = dict(new.system_metadata or {})
    if isinstance(meta.get(COORDS_KEY), dict) and meta[COORDS_KEY]:
        return  # new 已带 coords，无需回写
    old_meta = old.system_metadata or {}
    md_filename = str(old_meta.get(MD_FILENAME_KEY) or "")
    if not md_filename:
        return
    # memory/{project}/MEMORY.md | memory/{project}/daily_memory/... | memory/USER.md
    parts = md_filename.split("/")
    if len(parts) >= 3 and parts[0] == "memory" and parts[1] not in ("USER.md",):
        project = parts[1]
        old_coords = old_meta.get(COORDS_KEY)
        coords = dict(old_coords) if isinstance(old_coords, dict) else {}
        coords["project"] = project
        meta[COORDS_KEY] = coords
        new.system_metadata = meta
