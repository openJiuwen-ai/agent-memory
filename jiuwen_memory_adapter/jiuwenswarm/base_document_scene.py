# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared project binding and document defaults for independent memory scenes."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

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

    @staticmethod
    async def _bounded(awaitable: Any) -> list:
        try:
            return await asyncio.wait_for(awaitable, timeout=RECALL_TIMEOUT) or []
        except Exception as exc:
            logger.warning("Document memory prefetch failed: %s", type(exc).__name__)
            return []
