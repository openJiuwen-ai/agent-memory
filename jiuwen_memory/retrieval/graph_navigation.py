"""Bounded relation navigation and source evidence completion through DomainStore."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyRole,
)
from jiuwen_memory.storage.domain_store import DomainStore

_STOP = frozenset(
    (
        "what when where which who why how did does do is was were are the a an "
        "and or of to in on for his her their before after latest current all list"
    ).split()
)


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[\w]+", text.casefold())) - _STOP


@dataclass(frozen=True)
class GraphNavigationPolicy:
    seed_count: int = 2
    max_hops: int = 1
    max_neighbors: int = 4
    max_edges: int = 128
    max_reads: int = 64
    source_limit: int = 4
    include_original: bool = True
    strategy: str = "relational"
    ancestor_hops: int = 0
    context_radius: int = 0
    use_relations: bool = True
    initial_tokens: int = 2048
    source_vector_scopes: int = 16
    normalize_terms: bool = False
    source_role_balance: bool = False
    semantic_source_terms: bool = False
    analysis_token_reserve: int = 0
    initial_candidate_limit: int = 24
    summary_candidate_limit: int = 48
    source_candidate_limit: int = 96
    branch_width: int = 4
    summary_token_budget: int = 1024
    source_candidate_ratio: float = 0.0
    summary_candidate_ratio: float = 0.0
    initial_summary_fraction: float = 1 / 3
    adaptive_budget: bool = False
    adaptive_target_tokens: int = 0
    detail_source_hints: bool = False
    reserve_descendant_probes: bool = False
    refill_entry_probes: bool = False
    query_guided_entity_probe: bool = False
    chunk_source_hints: bool = False
    semantic_entry_terms: bool = False
    detail_keyword_entry: bool = False
    reserve_corroborated_entry: bool = False
    prefer_deferred_detail_keyword: bool = False
    prefer_coarse_keyword_entry: bool = False
    prefer_temporal_keyword_entry: bool = False
    reserve_temporal_anchor_entry: bool = False
    conversation_day_lookup: bool = False
    prefer_indexed_operands: bool = False
    source_clause_keywords: bool = False
    event_guided_entry: bool = False
    diversify_attributed_entry: bool = False
    probe_uncovered_clauses: bool = False
    semantic_endpoint_probe: str = "off"
    missing_clause_keyword_entry: bool = False
    single_slot_entry_consensus: bool = False
    relation_source_consensus: bool = False

    def __post_init__(self) -> None:
        if self.semantic_endpoint_probe not in ("off", "compound", "named"):
            raise ValidationError("semantic_endpoint_probe must be off, compound or named")
        if self.semantic_endpoint_probe != "off" and not self.probe_uncovered_clauses:
            raise ValidationError("semantic_endpoint_probe requires probe_uncovered_clauses")
        if (
            type(self.initial_summary_fraction) not in (int, float)
            or not math.isfinite(self.initial_summary_fraction)
            or not 0 < self.initial_summary_fraction <= 1
        ):
            raise ValidationError("initial_summary_fraction must be finite in (0, 1]")
        for name in ("source_candidate_ratio", "summary_candidate_ratio"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValidationError(f"{name} must be a finite ratio in [0, 1]")
        for name, cap in (
            ("seed_count", 16),
            ("max_hops", 3),
            ("max_neighbors", 32),
            ("max_edges", 1000),
            ("max_reads", 2048),
            ("source_limit", 128),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cap:
                raise ValidationError(f"graph_navigation.{name} must be an integer in [1, {cap}]")
        for name in (
            "include_original",
            "use_relations",
            "normalize_terms",
            "source_role_balance",
            "semantic_source_terms",
            "adaptive_budget",
            "detail_source_hints",
            "reserve_descendant_probes",
            "refill_entry_probes",
            "query_guided_entity_probe",
            "chunk_source_hints",
            "semantic_entry_terms",
            "detail_keyword_entry",
            "reserve_corroborated_entry",
            "prefer_deferred_detail_keyword",
            "prefer_coarse_keyword_entry",
            "prefer_temporal_keyword_entry",
            "reserve_temporal_anchor_entry",
            "conversation_day_lookup",
            "prefer_indexed_operands",
            "source_clause_keywords",
            "event_guided_entry",
            "diversify_attributed_entry",
            "probe_uncovered_clauses",
            "missing_clause_keyword_entry",
            "single_slot_entry_consensus",
            "relation_source_consensus",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValidationError(f"graph_navigation.{name} must be bool")
        if (
            type(self.adaptive_target_tokens) is not int
            or not 0 <= self.adaptive_target_tokens <= 32768
            or self.adaptive_target_tokens
            and not self.adaptive_budget
        ):
            raise ValidationError(
                "adaptive_target_tokens requires adaptive budgeting and a value in [0, 32768]"
            )
        if self.reserve_corroborated_entry and not self.refill_entry_probes:
            raise ValidationError("reserve_corroborated_entry requires refill_entry_probes")
        if self.prefer_deferred_detail_keyword and not self.refill_entry_probes:
            raise ValidationError("prefer_deferred_detail_keyword requires refill_entry_probes")
        if self.prefer_coarse_keyword_entry and not self.reserve_corroborated_entry:
            raise ValidationError("prefer_coarse_keyword_entry requires reserve_corroborated_entry")
        if self.prefer_temporal_keyword_entry and not self.reserve_corroborated_entry:
            raise ValidationError(
                "prefer_temporal_keyword_entry requires reserve_corroborated_entry"
            )
        if self.reserve_temporal_anchor_entry and not (
            self.reserve_corroborated_entry and self.event_guided_entry
        ):
            raise ValidationError(
                "reserve_temporal_anchor_entry requires entry metadata and reservation"
            )
        if self.diversify_attributed_entry and not self.event_guided_entry:
            raise ValidationError("diversify_attributed_entry requires event_guided_entry")
        if self.missing_clause_keyword_entry and not self.event_guided_entry:
            raise ValidationError("missing_clause_keyword_entry requires event_guided_entry")
        if self.single_slot_entry_consensus and not self.event_guided_entry:
            raise ValidationError("single_slot_entry_consensus requires event_guided_entry")
        if (
            type(self.analysis_token_reserve) is not int
            or not 0 <= self.analysis_token_reserve <= 4096
        ):
            raise ValidationError("analysis_token_reserve must be an integer in [0, 4096]")
        if self.strategy != "relational":
            raise ValidationError("graph_navigation supports relational strategy")
        for name, high in (
            ("initial_candidate_limit", 256),
            ("summary_candidate_limit", 256),
            ("source_candidate_limit", 512),
            ("branch_width", 16),
            ("summary_token_budget", 8192),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= high:
                raise ValidationError(f"invalid graph_navigation.{name}")
        if self.initial_candidate_limit > self.summary_candidate_limit:
            raise ValidationError("initial_candidate_limit must fit summary_candidate_limit")
        if type(self.source_vector_scopes) is not int or not 1 <= self.source_vector_scopes <= 64:
            raise ValidationError("graph_navigation.source_vector_scopes must be in [1, 64]")
        if type(self.initial_tokens) is not int or not 256 <= self.initial_tokens <= 32768:
            raise ValidationError("graph_navigation.initial_tokens must be in [256, 32768]")
        for name, cap in (("ancestor_hops", 64), ("context_radius", 2)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= cap:
                raise ValidationError(f"graph_navigation.{name} must be in [0, {cap}]")


class GraphEvidenceNavigator:
    def __init__(self, domain: DomainStore, policy: GraphNavigationPolicy) -> None:
        self._domain = domain
        self.policy = policy

    def candidate_limits(self, scope, query):
        from .graphmem_relation_plan import candidate_limits

        return candidate_limits(self._domain, self.policy, scope, query)

    def pack_closure(
        self, scope, query, candidates, packer, *, max_tokens, top_k, limits=None,
        entry_pool=None, recall_channels=None,
    ):
        from .graphmem_relational_navigation import pack_relational

        if not self.policy.include_original and query.hierarchy_role not in (
            HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT,
        ):
            raise ValidationError("summary-only navigation requires a summary hierarchy_role")
        return pack_relational(
            self._domain, self.policy, scope, query, candidates, packer,
            max_tokens=max_tokens, top_k=top_k, limits=limits, entry_pool=entry_pool,
            recall_channels=recall_channels,
        )


def build_graph_navigator(value: object, domain: DomainStore) -> GraphEvidenceNavigator | None:
    if value is None or value is False:
        return None
    if not isinstance(value, Mapping) or set(value) - asdict(GraphNavigationPolicy()).keys():
        raise ValidationError("invalid graph_navigation policy")
    return GraphEvidenceNavigator(domain, GraphNavigationPolicy(**value))
