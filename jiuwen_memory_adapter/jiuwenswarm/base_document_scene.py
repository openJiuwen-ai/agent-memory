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
_IS_CREDENTIAL_FILTER_FIELD = "system_metadata.is_credential"


def _is_credential_true_value(value: object) -> bool:
    """判断过滤值是否表示凭据真值（兼容 bool True 与大小写不敏感 'true'）。"""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _matches_credential_true(filters: Mapping[str, Any] | None) -> bool:
    """判断过滤条件是否等价于显式匹配凭据（is_credential == true）。"""
    if not isinstance(filters, Mapping) or len(filters) != 1:
        return False
    if "OR" in filters and isinstance(filters["OR"], list) and filters["OR"]:
        return any(
            isinstance(child, Mapping) and _matches_credential_true(child)
            for child in filters["OR"]
        )
    if _IS_CREDENTIAL_FILTER_FIELD not in filters:
        return False
    clause = filters[_IS_CREDENTIAL_FILTER_FIELD]
    if isinstance(clause, Mapping):
        if "eq" in clause:
            return _is_credential_true_value(clause.get("eq"))
        if "in" in clause and isinstance(clause.get("in"), (list, tuple, set)):
            return any(_is_credential_true_value(item) for item in clause["in"])
        return False
    return _is_credential_true_value(clause)


def _excludes_credentials(filters: Mapping[str, Any] | None) -> bool:
    """判断过滤条件是否显式排除了凭据（支持扁平 ne/not_in 及嵌套 AND/OR/NOT）。"""
    if not isinstance(filters, Mapping):
        return False
    if "AND" in filters and isinstance(filters["AND"], list):
        if any(
            isinstance(child, Mapping) and _excludes_credentials(child)
            for child in filters["AND"]
        ):
            return True
    if "OR" in filters and isinstance(filters["OR"], list) and filters["OR"]:
        if all(
            isinstance(child, Mapping) and _excludes_credentials(child)
            for child in filters["OR"]
        ):
            return True
    if "NOT" in filters and isinstance(filters["NOT"], Mapping):
        if _matches_credential_true(filters["NOT"]):
            return True
    clause = filters.get(_IS_CREDENTIAL_FILTER_FIELD)
    if not isinstance(clause, Mapping):
        return False
    if "ne" in clause and _is_credential_true_value(clause.get("ne")):
        return True
    if "not_in" in clause and isinstance(clause.get("not_in"), (list, tuple, set)):
        return any(_is_credential_true_value(item) for item in clause["not_in"])
    return False


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
        exclude_credentials = _excludes_credentials(filters)
        supplement = cls.project_recall(
            context,
            query,
            project,
            top_k=top_k,
            exclude_credentials=exclude_credentials,
        )
        ordinary = context.client.search(query, context.scope, top_k=top_k, filters=filters)
        if supplement is None:
            hits = await ordinary
            if exclude_credentials:
                hits = [hit for hit in hits if not hit.get("is_credential")]
            return hits[:top_k]
        primary, shared = await asyncio.gather(ordinary, supplement)
        ranked = sorted([*primary, *shared], key=lambda hit: hit.get("score") or 0, reverse=True)
        unique = {}
        for hit in ranked:
            if exclude_credentials and hit.get("is_credential"):
                continue
            unique.setdefault(hit["content"], hit)
        return list(unique.values())[:top_k]

    @classmethod
    def project_recall(
        cls,
        context: MemorySceneContext,
        query: str,
        project: str,
        *,
        top_k: int,
        exclude_credentials: bool = False,
    ):
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
            context,
            query,
            project,
            top_k=top_k,
            exclude_credentials=exclude_credentials,
        )

    @classmethod
    async def _search_collective_project(
        cls,
        context: MemorySceneContext,
        query: str,
        project: str,
        *,
        top_k: int,
        exclude_credentials: bool = False,
    ):
        filters: dict[str, Any] = {"system_metadata.collective_destination": {"eq": "project"}}
        if exclude_credentials:
            filters[_IS_CREDENTIAL_FILTER_FIELD] = {"ne": "true"}
        with context.client.collective_request({"project": project}):
            hits = await cls._bounded(
                context.client.search(query, context.scope, top_k=top_k, filters=filters),
                operation="project search",
            )
        if exclude_credentials:
            return [hit for hit in hits if not hit.get("is_credential")]
        return hits

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
