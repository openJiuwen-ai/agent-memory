# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Write checked experience-derived skills to a local skill library."""

import os
import tempfile
from pathlib import Path

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.abstractor_impl.skill_abstractor import (
    SKILL_NAME_MAX_LEN,
    SKILL_NAME_RE,
)


class SkillFileOutput:
    """Create skills atomically and leave existing files under the user's control."""

    def __init__(self, root: str = "", capabilities: list[str] | None = None) -> None:
        self._root = Path(root).resolve() if root else None
        self._capabilities = frozenset(capabilities or ())

    def write(self, unit: MemoryUnit) -> None:
        root = self._root
        if root is None or not ({"trial", "approved"} & set(unit.tags or ())):
            return
        metadata = unit.system_metadata
        name = str(metadata.get("skill_name", ""))
        if not SKILL_NAME_RE.fullmatch(name) or len(name) > SKILL_NAME_MAX_LEN:
            return
        required = {str(item) for item in metadata.get("capabilities") or ()}
        if self._capabilities and not required.issubset(self._capabilities):
            return
        if not unit.content.strip() or (root / name).exists():
            return
        distilled = root / "distilled"
        if distilled.is_symlink() or distilled.resolve().parent != root:
            raise ValidationError("Distilled skills directory must remain inside the skill library")
        directory = distilled / name
        if directory.is_symlink() or directory.resolve().parent != distilled:
            return
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "SKILL.md"
        if target.exists() or target.is_symlink():
            return

        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(unit.content)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass  # A concurrent writer already created the file.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
