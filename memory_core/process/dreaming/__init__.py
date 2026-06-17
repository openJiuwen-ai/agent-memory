# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dreaming: offline, cross-session memory consolidation.

Counterpart to ``process/extract`` (online, per-turn extraction): this package
runs in the background to consolidate already-stored memory across sessions —
a scheduler (orchestrator) driving a sweep pipeline (source → store).
"""

from memory_core.process.dreaming.orchestrator import DreamingOrchestrator
from memory_core.process.dreaming.source import (
    MessageStoreSessionSource,
    NormalizedSession,
    SessionSource,
)
from memory_core.process.dreaming.store import KnowledgeItem, MemoryUnitKnowledgeStore
from memory_core.process.dreaming.sweeper import Sweeper

__all__ = [
    "DreamingOrchestrator",
    "KnowledgeItem",
    "MemoryUnitKnowledgeStore",
    "MessageStoreSessionSource",
    "NormalizedSession",
    "SessionSource",
    "Sweeper",
]
