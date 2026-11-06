"""Exercise frozen derivation through record/recover, including crashes and retries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, NamedTuple
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    TaskRequest,
)

pytestmark = pytest.mark.unit


class DerivationFixture(NamedTuple):
    service: Any
    scope: Any
    event: Any
    write: Any
    read: Any
    units: Any
    lookup: Any


def configure(setup, contents):
    service, scope, write, read, _, units = setup
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)
    binding = service.task(scope, TaskRequest("bind", "bind", task["task_id"], 1,
                           connector_id="wb", context_id="session"), "owner", read)
    event = ContinuityEvent(task["task_id"], binding["task"]["bindings"][0]["binding_id"],
                            "event", "source")
    service.derivation_extractor = Mock()
    service.derivation_version = "extractor/model/1"
    service.derivation_extractor.extract.return_value = [
        MemoryUnit(id=str(i), scope=scope, segments=[Segment(content=text)])
        for i, text in enumerate(contents)
    ]

    def lookup(key):
        return [
            u for u in units.values()
            if u.system_metadata.get("continuity_output") == key
        ]

    return DerivationFixture(service, scope, event, write, read, units, lookup)


@pytest.mark.parametrize("contents", [[], ["one"], ["one", "two"], ["one", "one"]])
def test_record_freezes_and_replays_derivation(setup, contents):
    service, scope, event, write, read, units, lookup = configure(setup, contents)
    first = service.record(scope, event, "wb", read, write, query_output=lookup)
    assert first["status"] == first["derivation_status"] == "committed"
    assert len(first["derived"]) == len(set(contents))
    assert "build_plan" not in first, "private frozen content must not leak via receipt"
    service.derivation_extractor.extract.side_effect = AssertionError("must reuse plan")
    replay = service.record(scope, event, "wb", read, write, query_output=lookup)
    assert replay["derived"] == first["derived"]
    assert len(units) == 1 + len(set(contents))


def test_output_write_ack_loss_does_not_repeat_model_or_create(setup):
    service, scope, event, write, read, units, lookup = configure(setup, ["derived"])

    def interrupted(content, metadata):
        result = write(content, metadata)
        if metadata.get("continuity_output"):
            raise OSError("commit ack lost")
        return result

    first = service.record(scope, event, "wb", read, interrupted, query_output=lookup)
    assert first["status"] == "committed"
    assert first["derivation_status"] == "partial"
    outputs = [MemoryRef.of(u) for u in units.values()
               if u.system_metadata.get("continuity_output")]
    recovery = service.recover(scope, ContinuityRecovery(
        "derivation", first["operation_id"], outputs
    ), read)
    assert recovery["next_action"] == "retry_original_event"
    assert len(units) == 2
    replay = service.record(scope, event, "wb", read, write, query_output=lookup)
    assert replay["derivation_status"] == "committed"
    assert replay["derived"][0]["ref"] == asdict(outputs[0])
    service.derivation_extractor.extract.assert_called_once()
    assert len(units) == 2


def test_concurrent_record_retries_create_one_output(setup):
    service, scope, event, write, read, units, lookup = configure(setup, ["derived"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: service.record(scope, event, "wb", read, write, query_output=lookup),
            range(4),
        ))
    assert all(r["derivation_status"] == "committed" for r in results)
    assert len(units) == 2
    service.derivation_extractor.extract.assert_called_once()
