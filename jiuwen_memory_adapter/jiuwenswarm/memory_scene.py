# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scene contract and request-local services, independent of concrete strategies."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MemorySceneContext:
    """Request-local services; scenes never own the provider or its lifecycle."""

    client: Any
    scope: Any
    prepare: Callable[[str], str]
    video_enabled: Callable[[], bool]
    continuity_enabled: bool
    continuity_mode: str


class MemoryScene(Protocol):
    """Scene-specific behavior behind the existing MemoryProvider API."""

    def sdk_config_overrides(self) -> dict[str, Any]:
        """Return requirements applied after loading the SDK configuration."""
        ...

    def resolve_project(self, options: Mapping[str, Any]) -> str:
        """Resolve the project for the current turn."""
        ...

    def get_tool_schemas(self, context: MemorySceneContext) -> list[dict[str, Any]]:
        """Return independent model-visible tool definitions."""
        ...

    async def handle_tool_call(
        self, context: MemorySceneContext, tool_name: str, args: dict[str, Any],
    ) -> str:
        """Handle a tool call after the provider checks availability."""
        ...

    async def prefetch(self, context: MemorySceneContext, query: str, **kwargs: Any) -> str:
        """Build recall text using this scene's query and fallback rules."""
        ...

    def system_prompt(self, context: MemorySceneContext) -> str:
        """Describe the scene's tool usage to the model."""
        ...
