"""Different supported relation types survive a bounded neighbor quota."""

import pytest

from jiuwen_memory.construction.associator_impl.graphmem_topdown import (
    TopDownGraphMemAssociator,
    TopDownRelationPolicy,
)
from tests.unit.construction.graphmem_fixtures import Words
from tests.unit.construction.test_graphmem_topdown import day, summary

pytestmark = pytest.mark.unit


def test_time_edge_survives_competing_high_cosine_edges():
    nodes = [
        summary(x, "s" + x, x, window=day(8) if x == "a" else day(10) if x == "b" else None)
        for x in "abcdef"
    ]
    vectors = {
        "a": [1.0, 0.0, 0.0],
        "c": [0.99, 0.1, 0.0],
        "d": [0.98, 0.0, 0.1],
        "b": [0.4, 0.916, 0.0],
        "e": [0.39, 0.92, 0.01],
        "f": [0.38, 0.93, 0.02],
    }

    def make(balance):
        return TopDownGraphMemAssociator(
            Words(),
            TopDownRelationPolicy(root_neighbors=2, balance_attributes=balance),
            indexed_vectors=lambda u: vectors[u.id],
        ).associate_delta(nodes, {})

    old, new = make(False), make(True)
    assert ("a", "b") not in {(r.source_id, r.target_id) for r in old.relations}
    edge = next(r for r in new.relations if (r.source_id, r.target_id) == ("a", "b"))
    assert "temporal" in edge.metadata["attributes"]
    assert new.stats["model_calls"] == 0
    assert new.stats["max_degree"] <= 12
