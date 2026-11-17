# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable Markdown blocks for shared experience units."""

import base64
import re
from dataclasses import replace

from jiuwen_memory.common.type_def.memory import MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.scope import Scope

FILE_NAME = "EXPERIENCE.md"
_BLOCK = re.compile(r"<!-- experience:([A-Za-z0-9_=-]+) -->\n(.*?)\n<!-- /experience -->", re.S)


def path_for(scope: Scope) -> str:
    # A prefix handles empty values and avoids Windows reserved names. Lowercase
    # hex distinguishes IDs even on filesystems with case-insensitive paths.
    return f"experience/x{scope.org.encode().hex()}/x{scope.space.encode().hex()}/{FILE_NAME}"


def scope_from_path(path: str) -> Scope:
    prefix, org, space, filename = path.split("/")
    scope = Scope(org=bytes.fromhex(org[1:]).decode(), space=bytes.fromhex(space[1:]).decode())
    if prefix != "experience" or filename != FILE_NAME or path_for(scope) != path:
        raise ValueError("Invalid experience document path")
    return scope


def render(unit: MemoryUnit) -> str:
    if unit.scope != Scope(org=unit.scope.org, space=unit.scope.space):
        raise ValueError("Experience documents require an organization and space scope")
    if not unit.segments:
        raise ValueError("Experience documents require a body segment")
    first, *rest = unit.segments
    if "<!-- /experience -->" in first.content:
        raise ValueError("Experience body contains the reserved block terminator")
    metadata = dumps(replace(unit, segments=[replace(first, content=""), *rest]))
    payload = base64.urlsafe_b64encode(metadata).decode("ascii")
    return f"<!-- experience:{payload} -->\n{first.content}\n<!-- /experience -->\n\n"


def parse(path: str, source: str) -> dict[str, MemoryUnit]:
    scope = scope_from_path(path)
    units = {}
    end = 0
    for match in _BLOCK.finditer(source):
        if source[end:match.start()].strip():
            raise ValueError("Invalid experience block")
        end = match.end()
        unit = loads(base64.urlsafe_b64decode(match[1]))
        if unit is None or not unit.segments:
            raise ValueError("Invalid experience block metadata")
        if (
            unit.id in units
            or unit.scope != scope
            or unit.system_metadata.get("memory_type") != "experience"
        ):
            raise ValueError("Invalid experience block metadata")
        unit.segments[0] = replace(unit.segments[0], content=match[2])
        unit.system_metadata["md_filename"] = path
        units[unit.id] = unit
    if source[end:].strip():
        raise ValueError("Invalid experience block")
    return units
