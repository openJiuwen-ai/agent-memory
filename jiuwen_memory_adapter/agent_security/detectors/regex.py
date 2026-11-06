"""插件自有的有界正则检测器；不创建片段或还原入口。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time

from .._validation import has_exact_type
from ..inspection import InspectionError as ValidationError
from ..inspection import TextInspection
from .classifier import StaticPrivacyClassifier
from .taxonomy import SensitiveTaxonomy

# Existing SDK opaque token wire format, not an API or decryption capability.
PRIVACY_TOKEN = re.compile(r"\[JW-PRIVATE:([a-f0-9]{48})\]")


class RegexTextInspector:
    def __init__(self, classifier, *, timeout: float = 2.0, capacity: int = 10000):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or (not math.isfinite(timeout))
        ):
            raise ValidationError("invalid inspection budget")
        elif (
            not 0 < timeout <= 30
            or not has_exact_type(capacity, int)
            or (not 0 < capacity <= 100000)
        ):
            raise ValidationError("invalid inspection budget")
        self._classifier = classifier
        self._timeout = timeout
        self._capacity = capacity
        catalogue = [
            (
                r.definition.category,
                r.definition.pattern,
                r.definition.name,
                r.definition.group,
                r.definition.validator,
                r.level,
                r.pattern.flags,
            )
            for r in classifier.rules
        ]
        levels = sorted((name, level.rank) for name, level in classifier.taxonomy.levels.items())
        self._version = hashlib.sha256(
            json.dumps(
                ["mindvault-sdk-regex-pr406-v1", catalogue, levels, timeout, capacity],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        levels=None,
        rules=None,
        use_builtin_rules=None,
        category_levels=None,
        rule_overrides=None,
        timeout=2.0,
        capacity=10000,
    ):
        try:
            import regex
        except ImportError:
            raise ValidationError(
                "text inspection requires MindVaultSDK regex dependency"
            ) from None
        try:
            classifier = StaticPrivacyClassifier(
                SensitiveTaxonomy(levels),
                regex,
                rules=rules,
                use_builtin_rules=use_builtin_rules,
                category_levels=category_levels,
                rule_overrides=rule_overrides,
            )
            return cls(classifier, timeout=timeout, capacity=capacity)
        except (TypeError, ValueError, regex.error):
            raise ValidationError("invalid text inspection configuration") from None

    @property
    def version(self) -> str:
        return self._version

    def inspect(self, text: str, *, label: str = "") -> TextInspection:
        if not isinstance(text, str) or not isinstance(label, str):
            raise ValidationError("inspection requires text")
        if len(text.encode("utf-8")) > 262144 or len(label.encode("utf-8")) > 1024:
            raise ValidationError("inspection input too large")
        # 字段名作为语境参与凭据检测，但只替换实际值的跨度。
        prefix = f"{label}: " if label else ""
        value = prefix + text
        tokens = list(PRIVACY_TOKEN.finditer(value))
        protected = [m.span() for m in tokens]
        hits = self._classifier.detect(
            value,
            deadline=time.monotonic() + self._timeout,
            capacity=self._capacity,
            protected=sorted(protected),
        )
        # 仅忽略恰好等于固定掩码的命中，不能豁免其相邻或包裹的新秘密。
        hits = [h for h in hits if value[slice(h.start, h.end)] != "[PRIVATE]"]
        mark = self._classifier.classify(hits)
        spans = [
            (max(h.start, len(prefix)), h.end)
            for h in self._classifier.merge(hits)
            if h.end > len(prefix)
        ]
        # 稳定占位符也不暴露给外部接收方；不将其解释为原文或权限证明。
        spans.extend(m.span() for m in tokens)
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        parts, cursor = [], len(prefix)
        for start, end in merged:
            start = max(start, len(prefix))
            if end <= start:
                continue
            parts.extend((value[cursor:start], "[PRIVATE]"))
            cursor = end
        parts.append(value[cursor:])
        return TextInspection(mark.level_id, "".join(parts), mark.matched_rules, mark.categories)
