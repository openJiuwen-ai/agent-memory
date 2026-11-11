"""Durable evidence is excluded before any model or content mutation is called."""
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.construction.evolver import EvolveMode, EvolveResult
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", [EvolveMode.EXTRACT, EvolveMode.CONSOLIDATE,
                                  EvolveMode.ASSOCIATE, EvolveMode.FORGET])
def test_background_evolution_never_rewrites_relation_or_source(mode):
    scope = Scope(org="o", space="p")
    units = [MemoryUnit(id="relation", scope=scope, segments=[Segment(content="relation JSON")],
                        system_metadata={"continuity_kind": "relation"}),
             MemoryUnit(id="source", scope=scope, segments=[Segment(content="original")],
                        system_metadata={"continuity_event": "event-key"})]
    evolver = Mock()
    evolver.retains.return_value = False
    job = EvolveJob(scope, Mock(), evolver, mode)
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    result = job._evolve_group(units)
    evolver.evolve.assert_not_called()
    assert result == EvolveResult()
    ordinary = MemoryUnit(id="ordinary", scope=scope)
    evolver.evolve.return_value = EvolveResult()
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    job._evolve_group([*units, ordinary])
    evolver.evolve.assert_called_once_with([ordinary], mode)
