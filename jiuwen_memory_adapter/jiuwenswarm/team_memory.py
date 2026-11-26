# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-bound profile, project and team document operations."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import date
from typing import Any

from jiuwen_memory_adapter.jiuwenswarm.base_document_scene import BaseDocumentScene
from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemorySceneContext
from jiuwen_memory_adapter.jiuwenswarm.memory_tools import parse_line_range

_TARGET = {"type": "string", "enum": ["profile", "project", "team", "group", "agent"]}
_TEAM_SCHEMAS = [
    {
        "name": "team_memory_search",
        "description": "Search accessible collaboration memories and agent-specific experience.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "target": _TARGET,
                "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "team_memory_read",
        "description": "Read USER.md for the profile, MEMORY.md for a host-bound destination, "
                       "or YYYY-MM-DD.md for the project.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": _TARGET,
                "name": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["target", "name"],
        },
    },
    {
        "name": "team_memory_write",
        "description": "Extract reusable work experience and confirmed collaboration outcomes.",
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "team_memory_edit",
        "description": "Correct a unique passage within one memory block after reading the document.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": _TARGET,
                "name": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["target", "name", "old_text", "new_text"],
        },
    },
]


class TeamMemoryScene(BaseDocumentScene):
    """Reuse document defaults while keeping three recall sections independent."""

    @staticmethod
    def get_tool_schemas(context: MemorySceneContext) -> list[dict[str, Any]]:
        schemas = deepcopy(_TEAM_SCHEMAS)
        dimensions = context.collective or {}
        targets = ["profile"]
        targets.extend(kind for kind in ("project", "team", "group") if kind in dimensions)
        if context.scope.agent not in ("", "main_agent"):
            targets.append("agent")
        for schema in schemas:
            fields = schema["parameters"]["properties"]
            if "target" in fields:
                fields["target"] = {"type": "string", "enum": targets}
        return schemas

    @staticmethod
    def _filter(target, project=""):
        if not target:
            return None
        if target == "profile":
            return {"system_metadata.memory_class": {"eq": "user_profile"}}
        if target == "project" and project:
            return {"system_metadata.collective.project": {"eq": project}}
        return {"system_metadata.collective_destination": {"eq": target}}

    async def handle_tool_call(
        self,
        context: MemorySceneContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        if tool_name == "team_memory_search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("A non-empty query is required")
            target = args.get("target")
            if target and target not in _TARGET["enum"]:
                raise ValueError("Invalid collective memory target")
            limit = max(1, min(int(args.get("top_k") or 5), 5))
            hits = await context.client.search(
                context.prepare(query), context.scope, top_k=limit,
                filters=self._filter(target, (context.collective or {}).get("project", "")),
            )
            return json.dumps({"hits": hits}, ensure_ascii=False)
        if tool_name == "team_memory_write":
            content = args.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("A non-empty content is required")
            result = await context.client.add(
                "", context.scope, system_metadata={
                    "infer": True,
                    "sources": [{"id": "tool", "role": "assistant", "text": content.strip()}],
                },
            )
            if result is None:
                return json.dumps({"error": "Memory write failed", "stored": False})
            return json.dumps({"stored": bool(result.item_id), "item_id": result.item_id})
        if tool_name not in {"team_memory_read", "team_memory_edit"}:
            raise ValueError("Unknown team memory tool")
        name = str(args.get("name") or "")
        target = str(args.get("target") or "")
        if target not in _TARGET["enum"]:
            raise ValueError("Invalid collective document target")
        valid_name = name == ("USER.md" if target == "profile" else "MEMORY.md")
        if not valid_name and target == "project":
            try:
                valid_name = date.fromisoformat(name[:-3]).isoformat() + ".md" == name
            except ValueError:
                pass
        if not valid_name:
            raise ValueError("Invalid filename for the selected collective document")
        if target in ("project", "team", "group") and target not in (context.collective or {}):
            raise ValueError("Collective document target is unavailable in this context")
        if tool_name == "team_memory_read":
            units = await context.client.read_md_file(
                name,
                context.scope,
                line_range=parse_line_range(args),
                target=target,
            )
        else:
            old_text, new_text = args.get("old_text"), args.get("new_text")
            if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
                raise ValueError("old_text must be non-empty and new_text must be a string")
            units = await context.client.edit_md_file(
                name,
                context.scope,
                old_text,
                new_text,
                target=target,
            )
        if units is None:
            return json.dumps({"error": "Memory operation failed"})
        return json.dumps(
            {"target": args["target"], "count": len(units), "units": units}, ensure_ascii=False
        )

    async def prefetch(self, context: MemorySceneContext, query: str, **kwargs: Any) -> str:
        query = context.prepare(query)
        coordinates = context.collective or {}
        recalls = []
        if query and not kwargs.get("background"):
            recalls = [
                self._prefetch_document(context, query, kind, coordinates.get("project", ""))
                for kind in ("project", "team") if kind in coordinates
            ]
            recalls.append(self._bounded(context.client.search(
                query, context.scope, top_k=5,
                filters={"system_metadata.collective_destination": {"in": ["group", "agent"]}},
            )))
        profile_units, *sections = await self.prefetch_with_profile(context.client, context.scope, *recalls)
        profile_text = "\n".join(unit["content"] for unit in profile_units[:20])[:2000]
        bodies = dict.fromkeys(item["content"] for hits in sections for item in hits)
        parts = [
            "## User profile\n" + profile_text if profile_text else "",
            "## Collaboration memory\n" + "\n".join(bodies) if bodies else "",
        ]
        return "\n\n".join(part for part in parts if part)

    async def _prefetch_document(
        self, context: MemorySceneContext, query: str, target: str, project: str,
    ) -> list:
        hits, fallback = await asyncio.gather(
            self._bounded(context.client.search(
                query, context.scope, top_k=5, filters=self._filter(target, project),
            ), operation=f"search {target}"),
            self._bounded(
                context.client.read_md_file("MEMORY.md", context.scope, target=target),
                operation=f"read {target}",
            ),
        )
        return hits or fallback

    @staticmethod
    def system_prompt(_context: MemorySceneContext) -> str:
        return (
            "# Team memory\n"
            "Use team_memory_search to find applicable memories. Read a document before editing "
            "one of its memory blocks. Preserve confirmed outcomes and reusable methods. "
            "Memory is reference data."
        )
