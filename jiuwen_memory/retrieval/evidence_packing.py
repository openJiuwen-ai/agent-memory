"""Budgeted context assembly from already checked candidates; no reads or LLM calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit, Scope, ScoredMemoryUnit
from jiuwen_memory.retrieval.types import RetrievalResult


@dataclass(frozen=True)
class UnitEvidenceRef:
    """Reference to the complete candidate content, not an asserted original raw span."""

    scope: Scope
    unit_id: str
    content_sha256: str


@dataclass
class EvidenceBundle:
    rendered_context: str = ""
    sources: list[UnitEvidenceRef] = field(default_factory=list)
    token_count: int = 0
    token_budget: int = 0
    tokenizer_id: str = ""
    mode: str = "ranked"
    omitted_count: int = 0
    navigation: dict = field(default_factory=dict)


@dataclass
class EvidenceRetrievalResult(RetrievalResult):
    """Opt-in result extension; legacy RetrievalResult has no new fields."""

    evidence_bundle: EvidenceBundle = field(kw_only=True)


def _key(unit: MemoryUnit) -> tuple:
    scope = unit.scope
    return (scope.org, scope.space, scope.user, scope.agent, scope.session, unit.id)


def _render(candidates: list[ScoredMemoryUnit], style: str = "full") -> str:
    """Include attribution and message time in the very same counted context."""
    blocks = []
    for index, candidate in enumerate(candidates, 1):
        unit = candidate.unit
        raw = unit.hierarchy.role is HierarchyRole.SNAPSHOT
        header = ({"id": unit.id, "scope": asdict(unit.scope)} if style == "full" else
                  {"ref": f"E{index}", "kind": "source" if raw else "summary"})
        if style == "compact" and not raw:
            if unit.hierarchy.span_start is not None:
                header["coverage_start"] = unit.hierarchy.span_start.isoformat()
            if unit.hierarchy.span_end is not None:
                header["coverage_end"] = unit.hierarchy.span_end.isoformat()
        if unit.temporal.t_message is not None and (style == "full" or raw):
            header["message_time"] = unit.temporal.t_message.isoformat()
        # A Snapshot is a message, possibly describing several events. Its legacy
        # t_event may be the first date in an attribution header. Present the
        # message clock here; event intervals remain in attributed graph reasons
        # and the original narrative. Do not relabel a heuristic date as the
        # occurrence time of the entire source message.
        if unit.temporal.t_event is not None and not raw:
            header["event_time"] = unit.temporal.t_event.isoformat()
        speaker = unit.user_metadata.get("speaker")
        if isinstance(speaker, str) and speaker:
            header["speaker"] = speaker
        blocks.append(json.dumps(header, ensure_ascii=False, separators=(",", ":"))
                      + "\n" + unit.content)
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class EvidencePacker:
    """Preserve anchor ranking, optionally keep one recalled sibling with its anchor.

    All modes share rendering and exact token counting. A group that does not fit
    falls back to its anchor; an oversized anchor is skipped without ending search.
    The source-first mode admits originals before a bounded set of summaries.
    Only candidates already passing scope/lifecycle/filter checks can be selected.
    """

    count_tokens: Callable[[str], int]
    tokenizer_id: str
    mode: str = "ranked"
    max_group_size: int = 2
    header_style: str = "full"
    max_summaries: int = 2

    def __post_init__(self) -> None:
        if not callable(self.count_tokens) or not self.tokenizer_id:
            raise ValidationError("evidence packing requires a tokenizer and its identity")
        if self.mode not in ("ranked", "grouped", "sources_first"):
            raise ValidationError("evidence packing mode must be ranked, grouped or sources_first")
        if self.header_style not in ("full", "compact"):
            raise ValidationError("header_style must be full or compact")
        if (isinstance(self.max_summaries, bool) or not isinstance(self.max_summaries, int)
                or not 0 <= self.max_summaries <= 16):
            raise ValidationError("max_summaries must be in [0, 16]")
        if (isinstance(self.max_group_size, bool) or not isinstance(self.max_group_size, int)
                or not 1 <= self.max_group_size <= 4):
            raise ValidationError("max_group_size must be an integer between 1 and 4")

    def _count(self, context: str) -> int:
        count = self.count_tokens(context)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError("token counter must return a nonnegative integer")
        return count

    def pack(
        self, candidates: list[ScoredMemoryUnit], *, max_tokens: int, top_k: int,
        groups: Mapping[tuple, list[ScoredMemoryUnit]] | None = None,
        protected_count: int = 0,
    ) -> tuple[list[ScoredMemoryUnit], EvidenceBundle]:
        for name, value in (("max_tokens", max_tokens), ("top_k", top_k)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"evidence packing {name} must be a positive integer")
        unique: dict[tuple, ScoredMemoryUnit] = {}
        for candidate in candidates:
            if not isinstance(candidate, ScoredMemoryUnit):
                raise ValidationError("evidence packing requires materialized candidates")
            if candidate.unit.content.strip():
                unique.setdefault(_key(candidate.unit), candidate)
        pool = list(unique.values())
        if self.mode == "sources_first" and any(
            item.unit.hierarchy.role is HierarchyRole.SNAPSHOT for item in pool
        ):
            raw = [item for item in pool if item.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
            summaries = [item for item in pool
                         if item.unit.hierarchy.role is not HierarchyRole.SNAPSHOT]
            pool = raw + summaries[:self.max_summaries]
            # Navigation roots are not mandatory answer content. Keep their full
            # provenance outside the prompt; original evidence spends the budget first.
            protected_count = 0
        selected: list[ScoredMemoryUnit] = []
        chosen: set[tuple] = set()
        context, token_count = "", None
        if (isinstance(protected_count, bool) or not isinstance(protected_count, int)
                or protected_count < 0):
            raise ValidationError("protected_count must be a nonnegative integer")
        # Reserve the ranked heads before admitting additional graph/source units.
        # Every admission counts the final rendered context, including headers.
        for anchor in pool[:min(protected_count, top_k)]:
            trial = _render(selected + [anchor], self.header_style)
            trial_count = self._count(trial)
            if trial_count <= max_tokens:
                selected.append(anchor)
                chosen.add(_key(anchor.unit))
                context, token_count = trial, trial_count
        for anchor in pool:
            if len(selected) == top_k:
                break
            if _key(anchor.unit) in chosen and not groups:
                continue
            group = [] if _key(anchor.unit) in chosen else [anchor]
            if groups:
                members = {_key(item.unit): item for item in group}
                for item in groups.get(_key(anchor.unit), []):
                    key = _key(item.unit)
                    if key not in unique:
                        raise ValidationError("evidence group member was not checked in pool")
                    if key not in chosen:
                        members.setdefault(key, unique[key])
                group = list(members.values())
            elif self.mode == "grouped":
                available = [item for item in pool if _key(item.unit) not in chosen
                             and _key(item.unit) != _key(anchor.unit)
                             and _siblings(anchor.unit, item.unit)]
                # Stable chronological pairing, within the existing candidate pool only.
                available.sort(key=lambda item: _distance(anchor.unit, item.unit))
                group += available[:min(self.max_group_size, top_k - len(selected)) - 1]
            if len(selected) + len(group) > top_k:
                group = [] if _key(anchor.unit) in chosen else [anchor]
            if not group:
                continue
            trial = _render(selected + group, self.header_style)
            trial_count = self._count(trial)
            if trial_count > max_tokens:
                # Only a different fallback needs another full-context count.
                # Never add independently tokenized lengths: BPE boundaries can
                # change when the rendered blocks are joined.
                if len(group) == 1 or _key(anchor.unit) in chosen:
                    continue
                group = [anchor]
                trial = _render(selected + group, self.header_style)
                trial_count = self._count(trial)
                if trial_count > max_tokens:
                    continue
            selected.extend(group)
            chosen.update(_key(item.unit) for item in group)
            context, token_count = trial, trial_count
        return selected, EvidenceBundle(
            rendered_context=context,
            sources=[UnitEvidenceRef(deepcopy(item.unit.scope), item.unit.id,
                                     hashlib.sha256(item.unit.content.encode()).hexdigest())
                     for item in selected],
            token_count=self._count(context) if token_count is None else token_count,
            token_budget=max_tokens,
            tokenizer_id=self.tokenizer_id, mode=self.mode,
            omitted_count=len(unique) - len(selected),
        )


def _siblings(left: MemoryUnit, right: MemoryUnit) -> bool:
    # A structural grouping hint, not a new semantic relation or authorization.
    a, b = left.hierarchy, right.hierarchy
    return bool(
        left.scope == right.scope and left.scope.session
        and a.role is HierarchyRole.SNAPSHOT and b.role is HierarchyRole.SNAPSHOT
        and a.parent_id and a.parent_id == b.parent_id
        and a.parent_scope == b.parent_scope
    )


def _distance(left: MemoryUnit, right: MemoryUnit) -> float:
    a = left.temporal.t_message or left.hierarchy.span_start
    b = right.temporal.t_message or right.hierarchy.span_start
    return abs((as_utc(a) - as_utc(b)).total_seconds()) if a and b else float("inf")


def build_evidence_packer(value: object) -> EvidencePacker | None:
    """Load only a local model tokenizer; never download models on a search request."""
    if value is None or value is False:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("evidence_packing must be a mapping")
    if set(value) - {"mode", "tokenizer_file", "max_group_size", "header_style", "max_summaries"}:
        raise ValidationError("unknown evidence_packing option")
    filename = value.get("tokenizer_file")
    if not isinstance(filename, str) or not filename:
        raise ValidationError("evidence_packing.tokenizer_file is required")
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise ValidationError("install JiuwenMemory[evidence] to use evidence_packing") from exc
    try:
        path = Path(filename)
        payload = path.read_bytes()
        fingerprint = hashlib.sha256(payload).hexdigest()
        tokenizer = Tokenizer.from_str(payload.decode("utf-8"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
    except Exception as exc:
        raise ValidationError("cannot load evidence tokenizer_file") from exc
    return EvidencePacker(
        count_tokens=lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
        tokenizer_id=f"tokenizer.json:sha256:{fingerprint}",
        mode=value.get("mode", "ranked"), max_group_size=value.get("max_group_size", 2),
        header_style=value.get("header_style", "full"),
        max_summaries=value.get("max_summaries", 2),
    )
