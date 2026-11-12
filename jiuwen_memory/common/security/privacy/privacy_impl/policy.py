# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Configurable taxonomy and policy decisions adapted to the Jiuwen privacy boundary."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .static_privacy_classifier import Detection, PrivacyMark, StaticPrivacyClassifier


@dataclass(frozen=True)
class Level:
    name: str
    rank: int
    write: str = "redact"
    reveal: bool = True
    label: str = ""
    description: str = ""
    sync_mode: str = "none"  # full/none/index_only; enforcement is TODO
    retention_mode: str = "keep"  # keep/purge_after_ttl; enforcement is TODO
    retention_seconds: int = 0  # after soft deletion; enforcement is TODO
    share_mode: str = "no_share"  # shareable/internal_only/no_share; enforcement is TODO

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("invalid privacy level name")
        if not isinstance(self.label, str) or not isinstance(self.description, str):
            raise ValueError("invalid privacy level description")
        if not isinstance(self.reveal, bool):
            raise ValueError("privacy reveal must be bool")
        for value in (self.rank, self.retention_seconds):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("invalid privacy level integer")
        choices = (
            (self.write, {"allow", "redact"}),
            (self.sync_mode, {"full", "none", "index_only"}),
            (self.retention_mode, {"keep", "purge_after_ttl"}),
            (self.share_mode, {"shareable", "internal_only", "no_share"}),
        )
        for value, allowed in choices:
            if value not in allowed:
                raise ValueError("invalid privacy level policy")


DEFAULT_LEVELS = (
    {
        "name": "S1",
        "label": "公开",
        "rank": 1,
        "write": "allow",
        "sync_mode": "full",
        "share_mode": "shareable",
    },
    {"name": "S2", "label": "个人隐私", "rank": 2},
    {"name": "S3", "label": "凭据", "rank": 3},
)


@dataclass(frozen=True)
class PrivacyDecision:
    """Write-policy decision; storage and disclosure authorization remain separate."""

    level_id: str
    action: str
    allowed: bool = True


class SensitiveTaxonomy:
    """Configured names and ranks; no hardcoded ordering or automatic public fallback."""

    def __init__(self, levels: list[dict] | None = None) -> None:
        configured = [Level(**item) for item in (DEFAULT_LEVELS if levels is None else levels)]
        self.levels = {level.name: level for level in configured}
        if (
            not configured
            or len(self.levels) != len(configured)
            or len({level.rank for level in configured}) != len(configured)
        ):
            raise ValueError("invalid privacy taxonomy")
        self.default_level_id = min(configured, key=lambda level: level.rank).name

    def max_level(self, names: Iterable[str]) -> str:
        values = set(names)
        if not values:
            return self.default_level_id
        if not values <= self.levels.keys():
            raise ValueError("unknown privacy level")
        return max(values, key=lambda name: self.levels[name].rank)


class DictPrivacyManager:
    """Taxonomy and write decisions, without Engine, Store or authentication ownership."""

    def __init__(self, taxonomy: SensitiveTaxonomy, classifier: StaticPrivacyClassifier) -> None:
        self.taxonomy = taxonomy
        self.classifier = classifier

    def classify(self, hits: list[Detection]) -> PrivacyMark:
        """Return the static mark; classifier failure must not become a public result."""
        return self.classifier.classify(hits)

    def inherit_sensitivity(self, source_levels: Iterable[str]) -> str:
        """Preserve the strongest source level, including the current object's mark."""
        return self.taxonomy.max_level(source_levels)

    def decide_write(self, level: str) -> PrivacyDecision:
        return PrivacyDecision(level_id=level, action=self.taxonomy.levels[level].write)
