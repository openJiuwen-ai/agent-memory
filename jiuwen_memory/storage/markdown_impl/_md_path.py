# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""md 相对路径反推纯函数（F08 §3 路径规则逆运算）。

正运算（project/memory_class → md 路径）在 ``LocalMarkdownStore._md_path`` 与
``MarkdownStore.resolve_md_path``；逆运算（md 路径 → project/memory_class）统一住
在本模块，供看门狗（``local_watchdog._build_unit``）与数据面 edit 兜底
（``composite_domain_store._supersede_shadow_for_md``）复用，避免两处「同口径」
内联复刻漂移。

路径形态（相对 markdown root，F08 §3）：

- ``USER.md``                                   user_profile，跨 project 用户画像（root 顶层）
- ``memory/MEMORY.md``                          无 project long_term（跨项目可见，memory 根下）
- ``memory/{project}/MEMORY.md``                有 project long_term
- ``memory/daily_memory/YYYY-MM-DD.md``         无 project daily（跨项目可见）
- ``memory/{project}/daily_memory/YYYY-MM-DD.md`` 有 project daily
"""

from __future__ import annotations

import os

# 无 project 兜底值：空串 = 跨项目可见（对齐 SqliteDocumentShadowIndex._DEFAULT_PROJECT
# 与 LocalMarkdownStore._DEFAULT_PROJECT 语义）。
_DEFAULT_PROJECT = ""

# md 文件名 → memory_class 反推映射（对称 LocalMarkdownStore._PATH_MAP，F08 §3）。
_FILE_NAME_TO_CLASS: dict[str, str] = {
    "USER.md": "user_profile",
    "MEMORY.md": "long_term_memory",
}

# 未知 memory_class 兜底（对齐 LocalMarkdownStore._DEFAULT_CLASS）
_DEFAULT_CLASS = "long_term_memory"


def project_from_md_path(md_filename: str) -> str:
    """从 md 相对路径反推 project（F08 §3 路径规则）。

    有 project 段的最小形态是 ``memory/{project}/...``（至少 3 段），且第二段不能是
    ``daily_memory`` 目录。以下形态无 project，落空串（跨项目可见）：

    - ``memory/MEMORY.md``（无 project long_term，第二段是文件名 MEMORY.md）；
    - ``memory/daily_memory/日期.md``（无 project daily，第二段是目录名 daily_memory）；
    - ``USER.md``（root 顶层用户画像）与未知形态。
    """
    parts = md_filename.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "memory" and parts[1] != "daily_memory":
        return parts[1]
    return _DEFAULT_PROJECT


def class_from_md_path(md_filename: str) -> str:
    """从 md 相对路径反推 memory_class（F08 §3 映射，对称 LocalMarkdownStore._PATH_MAP）。"""
    normalized = md_filename.replace("\\", "/")
    if "daily_memory/" in normalized:
        return "daily_memory"
    base = os.path.basename(normalized)
    if base in _FILE_NAME_TO_CLASS:
        return _FILE_NAME_TO_CLASS[base]
    # 未知路径兜底 long_term_memory（与 LocalMarkdownStore._DEFAULT_CLASS 一致）
    return _DEFAULT_CLASS
