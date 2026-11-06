"""Impact propagation: bounded dependency review, three-tier aggregation.

When a relation changes (revised / revoked / newly contradicted), only the *dependent*
subgraph needs re-review, not the whole graph. This module walks that subgraph from the
changed node along confirmed edges and groups the affected references into:

- ``required``: dependents that are part of the current continuation's required evidence;
- ``candidate``: dependents that are only candidate suggestions;
- ``unchecked``: propagation hit the node/depth budget, so the survey is incomplete.

Direction follows the relation semantics: ``supports`` propagates forward (a change to the
supporting evidence affects its supported conclusion), while ``requires`` propagates
backward (a change to the required prerequisite affects the dependent that requires it).
Propagation reads every edge's authoritative relation truth (via ``resolve_edge``), never a
semantic top-k, and never mutates memory.
"""

from __future__ import annotations

from collections.abc import Callable

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import RelationStatus
from jiuwen_memory.storage.graph import GraphStore


def review_impact(
    scope: Scope,
    graph: GraphStore,
    resolve_edge: Callable[[str], tuple],
    seed: str,
    *,
    required: set[str],
    max_nodes: int = 50,
    max_depth: int = 4,
) -> dict:
    """Aggregate the subgraph affected by a change at ``seed`` into three review tiers.

    ``seed`` is the graph node key of the changed memory. ``required`` is the set of node
    keys that must remain available for the current continuation; dependents inside it land
    in the ``required`` tier, others in ``candidate``. Returns a ``gaps`` list that is
    non-empty when the survey was truncated (``unchecked``), so callers never mistake a
    bounded walk for a complete review.
    """
    required_items: list[str] = []
    candidate_items: list[str] = []
    unchecked = False
    visited = {seed}
    frontier = [seed]

    for _ in range(max_depth):
        if unchecked or not frontier:
            break
        next_frontier: list[str] = []
        for node in frontier:
            edges = graph.incident_edges(scope, node, limit=max_nodes + 1)
            if len(edges) > max_nodes:
                unchecked = True
                break
            for edge in edges:
                relation, _ = resolve_edge(edge.id)
                if relation.status != RelationStatus.CONFIRMED:
                    continue
                if relation.relation_type == "requires":
                    # requires is a reverse dependency: source requires target, so a change
                    # to the target (this node) affects the source.
                    if relation.target.key != node:
                        continue
                    affected = relation.source.key
                else:
                    # supports (and other forward edges): a change to the source affects
                    # the target.
                    if relation.source.key != node:
                        continue
                    affected = relation.target.key
                if affected in visited:
                    continue
                visited.add(affected)
                if len(visited) > max_nodes:
                    unchecked = True
                    break
                if affected in required:
                    required_items.append(affected)
                else:
                    candidate_items.append(affected)
                next_frontier.append(affected)
            if unchecked:
                break
        frontier = next_frontier
    if frontier and not unchecked:
        # The depth budget was exhausted while nodes remained to expand.
        unchecked = True

    return {
        "required": required_items,
        "candidate": candidate_items,
        "gaps": ["unchecked_dependencies"] if unchecked else [],
    }
