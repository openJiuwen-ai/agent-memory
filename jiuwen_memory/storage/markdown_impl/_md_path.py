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
import re
from datetime import date
from urllib.parse import quote, unquote

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MetadataValueType
from jiuwen_memory.common.type_def.memory_filter import COLLECTIVE_DOCUMENT_ROOT

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
    target = collective_document_target(md_filename)
    if target is not None:
        return target[1] if target[0] == "project" else ""
    team = team_from_md_path(md_filename)
    if team:
        return team_project(team)
    parts = md_filename.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "memory" and parts[1] != "daily_memory":
        return parts[1]
    return _DEFAULT_PROJECT


def _class_from_collective_destination(destination: str) -> str:
    return "user_profile" if destination == "user_profile" else f"{destination}_memory"


def class_from_md_path(md_filename: str) -> str:
    """从 md 相对路径反推 memory_class（F08 §3 映射，对称 LocalMarkdownStore._PATH_MAP）。"""
    target = collective_document_target(md_filename)
    if target is not None:
        if is_diary_filename(os.path.basename(md_filename.replace("\\", "/"))):
            return "daily_memory"
        return _class_from_collective_destination(target[0])
    if team_from_md_path(md_filename):
        return "team_memory"
    normalized = md_filename.replace("\\", "/")
    if "daily_memory/" in normalized:
        return "daily_memory"
    base = os.path.basename(normalized)
    if base in _FILE_NAME_TO_CLASS:
        return _FILE_NAME_TO_CLASS[base]
    # 未知路径兜底 long_term_memory（与 LocalMarkdownStore._DEFAULT_CLASS 一致）
    return _DEFAULT_CLASS


def team_from_md_path(md_filename: str) -> str:
    """Return the stable team key only for the canonical team document."""
    parts = md_filename.replace("\\", "/").split("/")
    if len(parts) == 4 and parts[:2] == ["memory", "teams"] and parts[3] == "MEMORY.md":
        team_project(parts[2])
        return parts[2]
    return ""


def team_project(team: str) -> str:
    """Validate a project--identity digest key and recover its project."""
    project, separator, digest = team.rpartition("--")
    if not separator or not project or re.fullmatch(r"[0-9a-f]{24}", digest) is None:
        raise ValidationError("Invalid team memory key")
    if project in {".", "..", "teams"} or any(char in project for char in "/\\\x00"):
        raise ValidationError("Invalid team memory key")
    return project


_COLLECTIVE_DIRS = {"project": "projects", "team": "teams", "group": "groups", "agent": "agents"}
_COLLECTIVE_KINDS = {directory: kind for kind, directory in _COLLECTIVE_DIRS.items()}
_WINDOWS_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", re.I)


def collective_landing_metadata(
    destination: str, key: str = "", *, shareable: bool | None = None,
) -> dict[str, MetadataValueType]:
    """Use the same destination defaults for routed writes and document recovery."""
    metadata: dict[str, MetadataValueType] = {
        "collective_destination": destination,
        "memory_class": _class_from_collective_destination(destination),
        "project": "", "team": "", "group": "",
        "shareable": destination in {"agent", "user_profile"} or shareable is True,
    }
    if destination in {"project", "team", "group"}:
        metadata[destination] = key
    return metadata


def is_diary_filename(filename: str) -> bool:
    if not filename.endswith(".md"):
        return False
    try:
        return f"{date.fromisoformat(filename[:-3]).isoformat()}.md" == filename
    except ValueError:
        return False


def collective_document_path(kind: str, key: str, filename: str = "MEMORY.md") -> str:
    """Map a trusted logical key to a single Windows-safe path segment."""
    if kind not in _COLLECTIVE_DIRS or not key or key in {".", ".."}:
        raise ValidationError("Invalid collective document coordinate")
    if any(char in "/\\" or ord(char) < 32 for char in key):
        raise ValidationError("Invalid collective document coordinate")
    if filename != "MEMORY.md":
        if kind != "project" or not is_diary_filename(filename):
            raise ValidationError("Invalid collective diary filename")
        filename = f"daily_memory/{filename}"
    segment = quote(key, safe="._-")
    if kind == "agent":
        # Reserve raw underscores for colons so the path reverses without aliasing agent IDs.
        segment = segment.replace("_", "%5F").replace("%3A", "_")
    if _WINDOWS_DEVICES.match(segment):
        segment = f"%{ord(segment[0]):02X}" + segment[1:]
    if segment.endswith("."):
        segment = segment[:-1] + "%2E"
    return f"{COLLECTIVE_DOCUMENT_ROOT}/{_COLLECTIVE_DIRS[kind]}/{segment}/{filename}"


def collective_document_target(md_filename: str) -> tuple[str, str] | None:
    """Return the dimension encoded by a canonical collective document."""
    path = md_filename.replace("\\", "/")
    prefix = f"{COLLECTIVE_DOCUMENT_ROOT}/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) == 3:
        directory, segment, filename = parts
    elif len(parts) == 4 and parts[2] == "daily_memory":
        directory, segment, _, filename = parts
    else:
        return None
    kind = _COLLECTIVE_KINDS.get(directory)
    if kind is None:
        return None
    key = unquote(segment.replace("_", ":") if kind == "agent" else segment)
    try:
        return (kind, key) if collective_document_path(kind, key, filename) == path else None
    except ValidationError:
        return None
