"""Session-local recursive aggregation with bounded calls and reusable subtrees."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY
from jiuwen_memory.common.graphmem_structure import (
    DETAIL_SPANS_KEY,
    RECURSIVE_VERSION,
    encode_child_details,
    is_recursive,
    node_height,
    valid_child_role,
)
from jiuwen_memory.common.llm import LLM
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    ContentLayers,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryTier,
    MemoryUnit,
    Segment,
    Temporal,
    validate_tree,
)
from jiuwen_memory.construction.hierarchy_composer import (
    TIME_PARENT_ROLES,
    HierarchyComposeRequest,
    HierarchyComposeResult,
    HierarchyComposerProducer,
)
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer

from .default_composer import (
    DefaultHierarchyComposer,
    _intersects,
    _node_key,
    _scope_key,
    _validate_leaf_spans,
    _validate_options,
    _validate_unit,
)
from .recursive_summary import (
    EVENT_PREFIX_REPAIRED,
    SOURCE_DAY_COMPACT_WIRE,
    RecursivePolicy,
    RecursiveSummarizer,
    SummaryOutputIncomplete,
    SummaryViewOverflow,
    records_for,
)


@dataclass
class RecursiveBatch:
    parents: list[MemoryUnit]
    leaves: list[MemoryUnit]
    reused: set[tuple]


def _source_order(unit: MemoryUnit) -> tuple:
    """Chronological, backend-independent order, including native ingest fragments.

    A multipart payload shares its message/ingestion timestamps. Store list order
    breaks those ties by random unit IDs, so preserving request order can scramble
    its text and invalidate unchanged groups on the next collection. Only native
    system metadata supplies a fragment ordinal; user metadata is not an order.
    """
    part, total = unit.system_metadata.get("_part"), unit.system_metadata.get("_parts")
    ordinal = part if (unit.source_ref and type(part) is int and type(total) is int
                       and 1 <= part <= total) else 0
    ingested = (as_utc(unit.temporal.t_ingest) if unit.temporal.t_ingest is not None
                else datetime.min.replace(tzinfo=UTC))
    return (as_utc(unit.hierarchy.span_start), ingested,
            unit.source_ref or unit.id, ordinal, unit.id)


class RecursiveHierarchyComposer(DefaultHierarchyComposer):
    """Keep the public TIME/time_span interface; role does not encode tree height.

    Explicit rebuild/append requests supply complete old subtrees. Unchanged
    groups retain their IDs and summaries; only changed parent links are written.
    The legacy periodic single-level scheduler is deliberately not advertised.
    """

    def __init__(self, index_builder: IndexBuilder, llm: LLM,
                 count_tokens: Callable[[str], int], *, tokenizer_id: str,
                 policy: RecursivePolicy | None = None, allow_cross_user: bool = False):
        self.index_builder = index_builder
        if type(allow_cross_user) is not bool:
            raise ValidationError("allow_cross_user must be bool")
        self.allow_cross_user = allow_cross_user
        self.profiles = {}
        self.summary = RecursiveSummarizer(llm, count_tokens, policy or RecursivePolicy(),
                                          tokenizer_id)

    def get_profile(self, kind):
        # The legacy scheduler assumes one fixed adjacent role per task.
        return None

    def _validate_request(self, request):
        if not isinstance(request, HierarchyComposeRequest):
            raise ValidationError("recursive composer requires HierarchyComposeRequest")
        if request.incremental is not None:
            raise ValidationError("recursive append uses explicit HIERARCHY with complete subtrees")
        _validate_options(request.options)
        if not isinstance(request.leaves, list) or not request.leaves:
            raise ValidationError("recursive composer requires source leaves")
        if not isinstance(request.existing_parents, list):
            raise ValidationError("existing_parents must be a list")
        for leaf in request.leaves:
            _validate_unit(leaf, HierarchyRole.SNAPSHOT)
            if leaf.hierarchy.child_ids or is_recursive(leaf):
                raise ValidationError("source Snapshot must be a leaf")
        for parent in request.existing_parents:
            _validate_unit(parent, TIME_PARENT_ROLES)
            if (parent.hierarchy.role not in request.options.parent_roles
                    or node_height(parent) < 1 or parent.scope != request.options.tree_home_scope
                    or not parent.hierarchy.child_ids):
                raise ValidationError(
                    "existing parent is outside the requested roles or home Scope")
        nodes = [*request.leaves, *request.existing_parents]
        validate_tree(nodes, allow_cross_user=self.allow_cross_user)
        indexed = {_node_key(node): node for node in nodes}
        parents = {_node_key(node) for node in request.existing_parents}
        for parent in request.existing_parents:
            children = []
            for i, uid in enumerate(parent.hierarchy.child_ids):
                cs = parent.hierarchy.child_scope_at(i, parent.scope)
                child = indexed.get((_scope_key(cs), uid))
                if child is None or not valid_child_role(parent, child):
                    raise ValidationError("recursive replacement requires all descendants")
                children.append(child)
            if not is_recursive(parent):
                # A complete legacy subtree can be rebuilt from its intact
                # sources. Its summaries cannot be reused under the new policy.
                continue
            if node_height(parent) != 1 + max(node_height(c) for c in children):
                raise ValidationError("recursive node height does not match its children")
            if parent.system_metadata.get("graphmem_child_roles") != [
                c.hierarchy.role.value for c in children
            ]:
                raise ValidationError("recursive child role projection does not match truth")
            for child in children:
                session = (json.dumps(_scope_key(child.scope))
                           if child.hierarchy.role is HierarchyRole.SNAPSHOT else
                           child.system_metadata.get("graphmem_session_scope"))
                if session != parent.system_metadata.get("graphmem_session_scope"):
                    raise ValidationError("recursive aggregation cannot cross sessions")
        for node in nodes:
            if node.hierarchy.parent_id:
                parent_key = (_scope_key(node.hierarchy.resolved_parent_scope(node.scope)),
                              node.hierarchy.parent_id)
                if parent_key not in parents:
                    raise ValidationError("recursive request omitted an existing ancestor")

    def build(self, request):
        self._validate_request(request)
        if request.existing_parents or any(n.hierarchy.parent_id for n in request.leaves):
            raise ValidationError("build cannot replace existing parent links")
        _validate_leaf_spans(request)
        return self._compose(request)

    def replace_in_span(self, request):
        self._validate_request(request)
        if request.options.span_start is None or request.options.span_end is None:
            raise ValidationError("recursive replacement requires a bounded span")
        for parent in request.existing_parents:
            if not parent.hierarchy.parent_id and not _intersects(parent, request.options):
                raise ValidationError("old root is outside the replacement span")
        _validate_leaf_spans(request)
        return self._compose(request)

    def _compose(self, request):
        candidate = deepcopy(request)
        batch = self.plan(candidate)
        validate_tree([*batch.parents, *batch.leaves], allow_cross_user=self.allow_cross_user)
        before = {_node_key(u): u for u in [*request.leaves, *request.existing_parents]}
        new = [u for u in batch.parents if _node_key(u) not in batch.reused]
        changed = [u for u in [*batch.leaves, *batch.parents]
                   if _node_key(u) in before and u != before[_node_key(u)]]
        retired = [u for u in candidate.existing_parents if _node_key(u) not in batch.reused]
        if not (new or changed or retired):
            return HierarchyComposeResult()
        # Reuse the established IndexBuilder-only partial-failure write protocol.
        return self._persist(new, changed, retired)

    def plan(self, request):
        """Pure preparation on copied inputs; every round reduces the frontier."""
        leaves = deepcopy(request.leaves)
        for leaf in leaves:
            leaf.hierarchy.parent_id, leaf.hierarchy.parent_scope = "", None
        available = {p.system_metadata.get("graphmem_child_digest"): p
                     for p in request.existing_parents
                     if is_recursive(p) and
                     p.system_metadata.get("graphmem_policy_id") == self.summary.policy_id}
        subdivided = {p.system_metadata["graphmem_split_input_digest"]: p
                      for p in available.values()
                      if "graphmem_split_input_digest" in p.system_metadata}
        old_nodes = {_node_key(p): p for p in request.existing_parents}
        parents, reused = [], set()
        sessions = defaultdict(list)
        for leaf in leaves:
            sessions[_scope_key(leaf.scope)].append(leaf)
        policy = self.summary.policy

        def restore_subdivision(root, children):
            # Reuse the entire verified subdivision, not only its root. The
            # digest covers original inputs; the frontier check prevents stale
            # metadata from admitting unrelated descendants or duplicate leaves.
            frontier = {_node_key(c): c for c in children}
            touched, copies = set(), {}

            def visit(old):
                key = _node_key(old)
                if key in copies:
                    raise ValidationError("duplicate node in recursive subdivision")
                parent = copies[key] = deepcopy(old)
                if parent.system_metadata.get("graphmem_policy_id") != self.summary.policy_id:
                    raise ValidationError("recursive subdivision has mixed policies")
                for i, uid in enumerate(parent.hierarchy.child_ids):
                    child_key = (_scope_key(parent.hierarchy.child_scope_at(i, parent.scope)), uid)
                    if child_key in frontier:
                        if child_key in touched:
                            raise ValidationError("duplicate input in recursive subdivision")
                        touched.add(child_key)
                    else:
                        child = old_nodes.get(child_key)
                        if child is None:
                            raise ValidationError("recursive subdivision differs from its inputs")
                        visit(child)

            visit(root)
            if touched != set(frontier):
                raise ValidationError("recursive subdivision omitted requested inputs")
            for parent in copies.values():
                for i, uid in enumerate(parent.hierarchy.child_ids):
                    key = (_scope_key(parent.hierarchy.child_scope_at(i, parent.scope)), uid)
                    child = frontier[key] if key in frontier else copies[key]
                    child.hierarchy.parent_id = parent.id
                    child.hierarchy.parent_scope = deepcopy(parent.scope)
            parent = copies[_node_key(root)]
            parent.hierarchy.parent_id, parent.hierarchy.parent_scope = "", None
            parents.extend(copies.values())
            reused.update(copies)
            return parent

        def parent_for(children, session):
            records = records_for(children)
            material = [self.summary.policy_id, _scope_key(request.options.tree_home_scope),
                        session, [(_node_key(c), c.hierarchy.span_start.isoformat(),
                                   c.hierarchy.span_end.isoformat()) for c in children], records]
            digest = hashlib.sha256(json.dumps(material, ensure_ascii=False,
                                               sort_keys=True).encode()).hexdigest()
            if digest in subdivided:
                return restore_subdivision(subdivided[digest], children)
            old = available.get(digest)
            if old is not None:
                parent = deepcopy(old)
                reused.add(_node_key(parent))
            else:
                try:
                    if len(children) == 1 and not self.summary.fits(records):
                        result = self.summary.oversized_source(records[0])
                    else:
                        result = self.summary.summarize(records)
                except (SummaryViewOverflow, SummaryOutputIncomplete):
                    # Output density can exceed the cap even when input fits.
                    # Subdivide only while making structural progress. A pair
                    # of summaries cannot be reduced further here. Dense raw
                    # sources may use bounded temporary fragments instead;
                    # neither recovery can publish an incomplete tree.
                    if len(children) == 1:
                        if (children[0].hierarchy.role is not HierarchyRole.SNAPSHOT
                                or len(records[0]["text"]) < 128
                                or not policy.max_source_refinements):
                            raise
                        result = self.summary.oversized_source(records[0], force_split=True)
                    elif len(children) == 2 and all(
                        c.hierarchy.role is not HierarchyRole.SNAPSHOT for c in children
                    ):
                        raise
                    else:
                        cut = len(children) // 2
                        reduced = [g[0] if len(g) == 1 and
                                   g[0].hierarchy.role is not HierarchyRole.SNAPSHOT
                                   else parent_for(g, session)
                                   for g in (children[:cut], children[cut:])]
                        parent = parent_for(reduced, session)
                        parent.system_metadata["graphmem_split_input_digest"] = digest
                        return parent
                height = 1 + max(node_height(c) for c in children)
                start = min(as_utc(c.hierarchy.span_start) for c in children)
                end = max(as_utc(c.hierarchy.span_end) for c in children)
                parent = MemoryUnit(
                    id=str(uuid4()), scope=deepcopy(request.options.tree_home_scope),
                    tier=MemoryTier.EPISODIC,
                    segments=[Segment(content=f"{start.isoformat()} ~ {end.isoformat()}"),
                              Segment(content=result["text"])],
                    layers=ContentLayers(l0=result["l0"], l1=result["l1"]),
                    entities=result["entities"], temporal=Temporal(t_ingest=datetime.now(UTC)),
                    system_metadata={**request.options.metadata,
                                     "graphmem_recursive_version": RECURSIVE_VERSION,
                                     "graphmem_height": str(height),
                                     "graphmem_session_scope": json.dumps(session),
                                     "graphmem_child_roles": [c.hierarchy.role.value
                                                              for c in children],
                                     "graphmem_child_digest": digest,
                                     "graphmem_policy_id": self.summary.policy_id,
                                     "graphmem_summary_status": "covered_inputs"},
                    hierarchy=HierarchyRef(
                        kind=HierarchyKind.TIME, role=HierarchyRole.TIME_SPAN,
                        child_ids=[c.id for c in children],
                        child_scopes=[deepcopy(c.scope) for c in children],
                        span_start=start, span_end=end),
                )
                if "semantics" in result:
                    parent.system_metadata[FEATURES_KEY] = result["semantics"].encode()
                    parent.system_metadata["graphmem_semantic_diagnostics"] = json.dumps(
                        result.get("semantic_diagnostics", {}), sort_keys=True)
                if result.get(EVENT_PREFIX_REPAIRED):
                    parent.system_metadata[EVENT_PREFIX_REPAIRED] = True
                if result.get(SOURCE_DAY_COMPACT_WIRE):
                    parent.system_metadata[SOURCE_DAY_COMPACT_WIRE] = True
                if "child_details" in result:
                    parent.system_metadata[DETAIL_SPANS_KEY] = encode_child_details(
                        parent, result["child_details"])
            parent.hierarchy.parent_id, parent.hierarchy.parent_scope = "", None
            for child in children:
                child.hierarchy.parent_id = parent.id
                child.hierarchy.parent_scope = deepcopy(parent.scope)
            parents.append(parent)
            return parent

        for session, sources in sorted(sessions.items()):
            current = sorted(sources, key=_source_order)
            # A long source is summarized independently, with recursive internal
            # map/reduce. Its original remains intact behind one direct link.
            current = [u if self.summary.fits(records_for([u])) else parent_for([u], session)
                       for u in current]
            # A single-source session needs a searchable summary too.
            if len(current) == 1 and current[0].hierarchy.role is HierarchyRole.SNAPSHOT:
                parent_for(current, session)
                continue
            while len(current) > 1:
                groups, group = [], []
                for unit in current:
                    trial = group + [unit]
                    if group and (len(trial) > policy.max_children
                                  or not self.summary.fits(records_for(trial))):
                        groups.append(group)
                        group = [unit]
                    else:
                        group = trial
                groups.append(group)
                if len(groups) >= len(current):
                    if any(u.hierarchy.role is HierarchyRole.SNAPSHOT for u in current):
                        # Two individually legal but long sources may not fit
                        # together. Compress each source once before retrying.
                        current = [parent_for([u], session)
                                   if u.hierarchy.role is HierarchyRole.SNAPSHOT else u
                                   for u in current]
                        continue
                    raise ValidationError("recursive grouping cannot reduce without truncation")
                current = [g[0] if len(g) == 1 else parent_for(g, session) for g in groups]
        return RecursiveBatch(parents, leaves, reused)


@HierarchyComposerProducer.register("graphmem_recursive")
def _build(config):
    from tokenizers import Tokenizer

    filename = config.get("tokenizer_file")
    if not isinstance(filename, str) or not filename:
        raise ValidationError("graphmem_recursive requires the model tokenizer_file")
    payload = Path(filename).read_bytes()
    tokenizer = Tokenizer.from_str(payload.decode())
    tokenizer.no_truncation()
    tokenizer.no_padding()
    policy = config.get("recursive_policy", {})
    if not isinstance(policy, dict):
        raise ValidationError("recursive_policy must be a mapping")
    try:
        parsed = RecursivePolicy(**policy)
    except TypeError as exc:
        raise ValidationError("invalid recursive_policy keys") from exc
    return RecursiveHierarchyComposer(
        IndexBuilderProducer.dep(config, "index_builder"), LlmProducer.dep(config),
        lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
        tokenizer_id=hashlib.sha256(payload).hexdigest(), policy=parsed,
        allow_cross_user=config.get("allow_cross_user", False),
    )
