"""Dedicated terminal return path; no restored MemoryUnit enters ordinary engine queries."""

import asyncio

from jiuwen_memory.common.security.privacy.base import HumanViewContent, PrivacyProtection
from jiuwen_memory.common.type_def import MemoryUnit


class HumanViewEngineMixin:
    @staticmethod
    async def render_human_view(
        privacy: PrivacyProtection, operation: str, current: MemoryUnit
    ) -> HumanViewContent:
        """Delegate one approved operation off the event loop and return only its human result."""
        return await asyncio.to_thread(privacy.render_disclosure, operation, current)
