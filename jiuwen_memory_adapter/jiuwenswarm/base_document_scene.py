# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared project binding and document defaults for independent memory scenes."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemorySceneContext

logger = logging.getLogger(__name__)
RECALL_TIMEOUT = 5.0


class BaseDocumentScene:
    """Reuse document mechanics without inheriting another scene's tools or policy."""

    def __init__(self, project: str) -> None:
        if not project or project in {".", ".."} or any(c in project for c in "/\\\x00"):
            raise ValueError("Document memory requires a project basename")
        self.project = project

    @staticmethod
    def sdk_config_overrides() -> dict[str, Any]:
        """Require document writes after loading the SDK baseline."""
        return {"globals": {"write_document": True}}

    def resolve_project(self, _options: Mapping[str, Any]) -> str:
        """Use the host-bound project for every turn."""
        return self.project

    @classmethod
    async def search_project_memory(cls, context, query, *, top_k, filters, project):
        """Keep ordinary search semantics while adding an authenticated project recall."""
        supplement = cls.project_recall(context, query, project, top_k=top_k)
        ordinary = context.client.search(query, context.scope, top_k=top_k, filters=filters)
        if supplement is None:
            return await ordinary
        primary, shared = await asyncio.gather(ordinary, supplement)
        ranked = sorted([*primary, *shared], key=lambda hit: hit.get("score") or 0, reverse=True)
        unique = {}
        for hit in ranked:
            unique.setdefault(hit["content"], hit)
        return list(unique.values())[:top_k]

    @classmethod
    def project_recall(cls, context: MemorySceneContext, query: str, project: str, *, top_k: int):
        """Return the optional recall for the host-bound project."""
        coordinates = context.collective or {}
        if not project or coordinates.get("project") != project or coordinates.get("team"):
            return None
        if context.scope.session or not isinstance(query, str):
            return None
        query = context.prepare(query)
        if not query.strip():
            return None
        return cls._search_collective_project(
            context.client, context.scope, query, project, top_k=top_k,
        )

    @classmethod
    async def _search_collective_project(cls, client, scope, query, project, *, top_k):
        with client.collective_request({"project": project}):
            return await cls._bounded(client.search(
                query, scope, top_k=top_k,
                filters={"system_metadata.collective_destination": {"eq": "project"}},
            ), operation="project search")

    @classmethod
    async def prefetch_with_profile(cls, client: Any, scope: Any, *recalls: Any) -> list:
        profile = cls._bounded(client.read_md_file("USER.md", scope, target="profile"))
        return await asyncio.gather(profile, *recalls)

    @staticmethod
    async def _bounded(awaitable: Any, *, operation: str = "") -> list:
        try:
            return await asyncio.wait_for(awaitable, timeout=RECALL_TIMEOUT) or []
        except Exception as exc:
            location = f" ({operation})" if operation else ""
            logger.warning("Document memory prefetch failed%s: %s", location, type(exc).__name__)
            return []
