# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared, bounded classification and redaction detection adapted from the static classifier."""

from __future__ import annotations

import re
import time
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from ..inspection import InspectionError as ValidationError
from .rules import DEFAULT_RULES, VALIDATORS, DetectRule
from .taxonomy import SensitiveTaxonomy

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")


def outside_tokens(
    start: int, end: int, protected: list[tuple[int, int]], starts: list[int]
) -> Iterator[tuple[int, int]]:
    """Subtract reserved opaque references while retaining adjacent generated secrets."""
    index = max(0, bisect_right(starts, start) - 1)
    cursor = start
    while index < len(protected) and protected[index][0] < end:
        left, right = protected[index]
        if cursor < left:
            yield cursor, min(left, end)
        cursor = max(cursor, right)
        index += 1
    if cursor < end:
        yield cursor, end


@dataclass(frozen=True)
class CompiledRule:
    definition: DetectRule
    pattern: Any
    level: str | None


@dataclass(frozen=True)
class Detection:
    """Span and server configuration identifiers; never retains a matched original value."""

    start: int
    end: int
    level: str | None
    rule_names: tuple[str, ...]
    categories: tuple[str, ...]


@dataclass(frozen=True)
class PrivacyMark:
    """Static classification result, including bounded non-content evidence."""

    level_id: str
    matched_rules: tuple[str, ...]
    categories: tuple[str, ...]
    classified_by: str = "static"


class StaticPrivacyClassifier:
    """Highest-rank classification and complete span detection share one rule catalogue.

    Legacy ``rules`` replace builtins unless ``use_builtin_rules=True`` explicitly
    requests composition. User rules never short-circuit builtin credential hits.
    """

    def __init__(
        self,
        taxonomy: SensitiveTaxonomy,
        bounded_regex,
        *,
        rules: list[dict] | None = None,
        use_builtin_rules: bool | None = None,
        category_levels: dict[str, str] | None = None,
        rule_overrides: dict[str, dict] | None = None,
    ) -> None:
        self.taxonomy = taxonomy
        if use_builtin_rules is not None and not isinstance(use_builtin_rules, bool):
            raise ValueError("use_builtin_rules must be bool")
        if category_levels is not None and not isinstance(category_levels, dict):
            raise ValueError("invalid category mapping")
        self.category_levels = {"pii.*": "S2", "credential.*": "S3"}
        for category, level in (category_levels or {}).items():
            label = (
                category[:-2] if isinstance(category, str) and category.endswith(".*") else category
            )
            if (
                not isinstance(label, str)
                or not _IDENTIFIER.fullmatch(label)
                or level not in taxonomy.levels
            ):
                raise ValueError("invalid category mapping")
            self.category_levels[category] = level
        if rule_overrides is not None and not isinstance(rule_overrides, dict):
            raise ValueError("invalid builtin overrides")
        known = {rule.name for rule in DEFAULT_RULES}
        for name, fields in (rule_overrides or {}).items():
            if (
                name not in known
                or not isinstance(fields, dict)
                or fields.keys() - {"enabled", "validator"}
            ):
                raise ValueError("invalid builtin override")
            if "enabled" in fields and not isinstance(fields["enabled"], bool):
                raise ValueError("invalid builtin switch")
            validator = fields.get("validator")
            if validator is not None and (
                not isinstance(validator, str) or validator not in VALIDATORS
            ):
                raise ValueError("invalid builtin validator")
        self.rules: list[CompiledRule] = []
        builtin = rules is None if use_builtin_rules is None else use_builtin_rules
        if builtin:
            for rule in DEFAULT_RULES:
                definition = replace(rule, **(rule_overrides or {}).get(rule.name, {}))
                self._compile(definition, None, bounded_regex, builtin=True)
        if rules is not None and not isinstance(rules, list):
            raise ValueError("rules must be a list")
        for index, raw in enumerate(rules or []):
            self._custom(raw, index, bounded_regex)

    def _level_for(self, category: str) -> str | None:
        if category in self.category_levels:
            return self.category_levels[category]
        prefixes = [
            key
            for key in self.category_levels
            if key.endswith(".*") and category.startswith(key[:-1])
        ]
        if prefixes:
            return self.category_levels[max(prefixes, key=len)]
        return None

    def _compile(
        self,
        rule: DetectRule,
        level: str | None,
        regex,
        *,
        builtin: bool = False,
        colleague_patterns: bool = False,
    ):
        for identifier in (rule.name, rule.category):
            if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
                raise ValueError("invalid detection rule")
        if not isinstance(rule.enabled, bool):
            raise ValueError("invalid detection rule")
        if rule.validator is not None:
            if not isinstance(rule.validator, str) or rule.validator not in VALIDATORS:
                raise ValueError("invalid detection rule")
        if not isinstance(rule.pattern, str) or not rule.pattern:
            raise ValueError("invalid detection rule")
        if not isinstance(rule.group, (str, int)) or isinstance(rule.group, bool):
            raise ValueError("invalid detection rule")
        if not rule.enabled:
            return
        flags = (regex.IGNORECASE | regex.MULTILINE) if builtin or colleague_patterns else 0
        compiled = regex.compile(rule.pattern, flags)
        if isinstance(rule.group, str) and rule.group not in compiled.groupindex:
            raise ValueError("invalid detection group")
        if isinstance(rule.group, int) and not 0 <= rule.group <= compiled.groups:
            raise ValueError("invalid detection group")
        resolved = level if level is not None else self._level_for(rule.category)
        if resolved is not None and resolved not in self.taxonomy.levels:
            raise ValueError("unknown privacy level")
        # Unmapped builtins (injection) produce evidence only. Explicit custom rules
        # must choose a valid level, directly or through their category mapping.
        if resolved is None and not builtin:
            raise ValueError("custom rule requires a level or mapped category")
        self.rules.append(CompiledRule(rule, compiled, resolved))

    def _custom(self, raw: dict, index: int, regex) -> None:
        if not isinstance(raw, dict):
            raise ValueError("invalid custom rule")
        level = raw.get("level", raw.get("level_id"))
        if "level" in raw and "level_id" in raw and raw["level"] != raw["level_id"]:
            raise ValueError("conflicting custom levels")
        if level is not None and level not in self.taxonomy.levels:
            raise ValueError("unknown privacy level")
        patterns = raw.get("patterns", [])
        keywords = raw.get("keywords", [])
        colleague_patterns = "patterns" in raw and "pattern" not in raw
        if not isinstance(patterns, list) or not isinstance(keywords, list):
            raise ValueError("invalid custom rule patterns or keywords")
        if "pattern" in raw:
            if patterns:
                raise ValueError("use pattern or patterns")
            patterns = [raw["pattern"]]
        if any(not isinstance(word, str) or not word for word in keywords):
            raise ValueError("invalid custom keyword")
        if not patterns and not keywords:
            raise ValueError("custom rule has no matcher")
        definition = {
            "name": raw.get("name", f"custom.{index}"),
            "category": raw.get("category", "custom.rule"),
            "enabled": raw.get("enabled", True),
            "validator": raw.get("validator"),
        }
        for pattern in patterns:
            self._compile(
                DetectRule(pattern=pattern, group=raw.get("group", 0), **definition),
                level,
                regex,
                colleague_patterns=colleague_patterns,
            )
        for keyword in keywords:
            self._compile(
                DetectRule(pattern="(?i:" + regex.escape(keyword) + ")", **definition), level, regex
            )

    def detect(
        self,
        text: str,
        *,
        deadline: float,
        capacity: int,
        protected: list[tuple[int, int]],
    ) -> list[Detection]:
        starts = [span[0] for span in protected]
        hits = []
        for rule in self.rules:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValidationError("privacy regex timeout")
            try:
                for match in rule.pattern.finditer(text, timeout=remaining):
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    start, end = match.span(rule.definition.group)
                    if start < 0 or end <= start:
                        continue
                    validator = rule.definition.validator
                    if validator is not None and not VALIDATORS[validator](
                        match.group(rule.definition.group)
                    ):
                        continue
                    for left, right in outside_tokens(start, end, protected, starts):
                        hits.append(
                            Detection(
                                left,
                                right,
                                rule.level,
                                (rule.definition.name,),
                                (rule.definition.category,),
                            )
                        )
                        if len(hits) > capacity * max(1, len(self.rules)):
                            raise ValidationError("privacy detection capacity exceeded")
            except TimeoutError:
                raise ValidationError("privacy regex timeout") from None
        return hits

    def classify(self, hits: list[Detection]) -> PrivacyMark:
        return PrivacyMark(
            self.taxonomy.max_level(hit.level for hit in hits if hit.level is not None),
            tuple(sorted({name for hit in hits for name in hit.rule_names})[:64]),
            tuple(sorted({category for hit in hits for category in hit.categories})[:64]),
        )

    def merge(self, hits: list[Detection]) -> list[Detection]:
        merged: list[Detection] = []
        for hit in sorted(
            (hit for hit in hits if hit.level is not None),
            key=lambda hit: (hit.start, hit.end, hit.level),
        ):
            if merged and hit.start < merged[-1].end:
                previous = merged.pop()
                merged.append(
                    Detection(
                        previous.start,
                        max(previous.end, hit.end),
                        self.taxonomy.max_level((previous.level, hit.level)),
                        tuple(sorted(set(previous.rule_names + hit.rule_names))),
                        tuple(sorted(set(previous.categories + hit.categories))),
                    )
                )
            else:
                merged.append(hit)
        return merged
