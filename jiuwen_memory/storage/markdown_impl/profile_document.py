# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Whole-file Markdown projection of a single user's Dreaming profile."""

import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    FanOutCandidate,
    FilterClause,
    FilterExpr,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    PredicateCandidate,
    Scope,
)
from jiuwen_memory.common.type_def.memory_filter import matches_memory_unit

MEMORY_CLASS = "dreamed_user_profile"
BASENAME = "user_profile.md"
_CURSOR = re.compile(r"\A<!-- profile-cursor: ([0-9]+) -->\n\n")
DIRECTORY = "memory/dreaming"
_LOCAL_OWNER = Scope(org="__default__", user="__default__")
_LOCAL_PATH = f"{DIRECTORY}/{BASENAME}"
_PATH = re.compile(rf"{DIRECTORY}/[0-9a-f]{{64}}/{re.escape(BASENAME)}")
_LEGACY_PATH = re.compile(rf"users/[0-9a-f]{{64}}/{DIRECTORY}/{re.escape(BASENAME)}")


def profile_id(scope: Scope) -> str:
    if (
        not scope.org or not scope.user
        or scope.agent or scope.session
    ):
        raise ValidationError(
            "A profile requires an organization and user, with no agent or session"
        )
    coords = json.dumps([scope.org, scope.space, scope.user], ensure_ascii=False)
    return hashlib.sha256(coords.encode("utf-8")).hexdigest()


def relative_path(scope: Scope) -> str:
    if scope == _LOCAL_OWNER:
        return _LOCAL_PATH
    return f"{DIRECTORY}/{profile_id(scope)}/{BASENAME}"


def candidates(scope: Scope) -> FanOutCandidate:
    profile_id(scope)
    return FanOutCandidate(
        require_empty=frozenset({"space"}) if not scope.space else frozenset(),
        child=PredicateCandidate(filters=[
            FilterClause("system_metadata.memory_class", FilterOp.NE, MEMORY_CLASS),
            FilterClause("system_metadata.memory_type", FilterOp.NOT_IN, ["experience", "skill"]),
        ]),
    )


def accepts_input(unit: MemoryUnit) -> bool:
    """Only project-independent personal memory may be copied to the profile."""
    metadata = unit.system_metadata
    path = str(metadata.get("md_filename", "")).replace("\\", "/")
    if (
        path.startswith(("memory/team_memory/", "memory/teams/"))
        or metadata.get("team")
        or metadata.get("group")
    ):
        return False
    destination = metadata.get("collective_destination")
    if destination:
        return (
            destination == "user_profile"
            and metadata.get("memory_class") == "user_profile"
            and metadata.get("shareable") is True
            and not metadata.get("project")
            and not unit.scope.agent
            and not unit.scope.session
        )
    if metadata.get("memory_class") == "user_profile":
        return True
    return metadata.get("memory_scene") != "team" and metadata.get("project") in (
        None, "", "default",
    )


def eligible(unit: MemoryUnit, filters: FilterExpr) -> bool:
    return (
        unit.lifecycle == LifecycleState.ACTIVE
        and unit.system_metadata.get("middle") != "true"
        and not unit.system_metadata.get("continuity_event")
        and unit.system_metadata.get("continuity_kind") != "relation"
        and accepts_input(unit)
        and matches_memory_unit(unit, filters)
    )


def is_profile_path(path: str) -> bool:
    relative = path.replace("\\", "/")
    return relative == _LOCAL_PATH or bool(
        _PATH.fullmatch(relative) or _LEGACY_PATH.fullmatch(relative)
    )


def absolute_path(root: str, relative: str) -> Path:
    directory = Path(root).resolve()
    path = directory / relative
    if not is_profile_path(relative) or path.resolve() != path:
        raise ValidationError("Invalid profile document path")
    return path


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def read_or_move_legacy(root: str, scope: Scope) -> str | None:
    """Read the current profile, moving the previous layout when necessary."""
    path = absolute_path(root, relative_path(scope))
    text = read(path)
    if text is not None:
        return text
    owner_id = profile_id(scope)
    older_paths = [f"users/{owner_id}/{DIRECTORY}/{BASENAME}"]
    if scope == _LOCAL_OWNER:
        older_paths.insert(0, f"{DIRECTORY}/{owner_id}/{BASENAME}")
    for relative in older_paths:
        legacy = absolute_path(root, relative)
        text = read(legacy)
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(path)
            return text
    return None


def replace_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(content.encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def unpack(text: str | None) -> tuple[str, int]:
    if text is None:
        return "", 0
    match = _CURSOR.match(text)
    return (text[match.end():], int(match[1])) if match else (text, 0)


def pack(body: str, cursor: int) -> str:
    return f"<!-- profile-cursor: {cursor} -->\n\n{body}"
