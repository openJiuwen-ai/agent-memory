"""Explicit event_time across batch, background ingest and verbatim publication."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwen_memory.api import BatchWriteItem, Modality, Scope, assemble_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.control.engine_impl.agent_engine import AgentEngine
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.types import SpaceSpec
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

EVENT_TIME = datetime(2024, 2, 3, 12, tzinfo=UTC)
OTHER_TIME = datetime(2023, 1, 1, tzinfo=UTC)
NAIVE_TIME = datetime(2024, 2, 3, 12)


def _security(actor, role=Role.USER):
    return internal_context(ScopedAuthenticator(actor, role=role))


@pytest.fixture
def env(tmp_path):
    config = yaml.safe_load(Path("examples/role_memory/server.yaml").read_text())
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    runtime = assemble_runtime(config=config)
    project = Scope(org="demo", space="role-project")
    owner = Scope(org="demo", user="owner")
    runtime.api.create_space(
        SpaceSpec(org="demo", space="role-project", owner=owner),
        security=_security(Scope(org="demo", user="ops"), Role.ADMIN),
    )
    yield runtime.api, project, _security(owner)
    runtime.close()


def test_batch_item_event_time_overrides_batch_default(env):
    api, project, security = env
    result = api.batch_add(
        [
            BatchWriteItem(content="uses default"),
            BatchWriteItem(content="uses own", event_time=OTHER_TIME),
        ],
        project,
        security=security,
        event_time=EVENT_TIME,
    )
    actual_times = [outcome.units[0].temporal.t_event for outcome in result.outcomes]
    assert actual_times == [EVENT_TIME, OTHER_TIME]


def test_batch_event_time_is_independent_of_message_time(env):
    api, project, security = env
    result = api.batch_add(
        [BatchWriteItem(content="x", occurred_at=OTHER_TIME, event_time=EVENT_TIME)],
        project,
        security=security,
    )
    temporal = result.outcomes[0].units[0].temporal
    assert temporal.t_event == EVENT_TIME
    assert temporal.t_message == OTHER_TIME


def test_batch_rejects_naive_item_without_failing_other_items(env):
    api, project, security = env
    result = api.batch_add(
        [BatchWriteItem(content="bad", event_time=NAIVE_TIME), BatchWriteItem(content="good")],
        project,
        security=security,
    )
    assert result.outcomes[0].error_type == "ValidationError"
    assert result.outcomes[1].units, "one invalid item must not fail the batch"


@pytest.mark.parametrize("value", [NAIVE_TIME, "2024-02-03T12:00:00+00:00"])
def test_batch_rejects_invalid_default_event_time(env, value):
    api, project, security = env
    with pytest.raises(ValidationError, match="event_time"):
        api.batch_add([BatchWriteItem(content="x")], project, security=security, event_time=value)


@pytest.mark.parametrize("engine", [AgentEngine, CloudEngine, InMemoryEngine])
def test_engine_batch_write_forwards_event_time(engine):
    stub = Mock(write=AsyncMock(return_value=[]))
    item = BatchWriteItem(content="x", scope=Scope(org="o"), event_time=EVENT_TIME)
    asyncio.run(engine.batch_write(stub, [item]))
    assert stub.write.await_args.kwargs["event_time"] == EVENT_TIME


def test_submit_ingest_rejects_naive_event_time_before_enqueue(env, monkeypatch):
    api, project, security = env
    submit = Mock()
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    monkeypatch.setattr(api._ingest_jobs, "submit", submit)
    with pytest.raises(ValidationError, match="event_time"):
        api.submit_ingest(
            "x", project, Modality.TEXT, security=security,
            payload_id="p", source_ref="file:///x", event_time=NAIVE_TIME,
        )
    submit.assert_not_called()


def test_submit_ingest_passes_event_time_to_background_add(env, monkeypatch):
    api, project, security = env
    submit = Mock()
    # White-box fixture/fault injection; do not expose a production test API.
    # pylint: disable-next=protected-access
    monkeypatch.setattr(api._ingest_jobs, "submit", submit)
    api.submit_ingest(
        "x", project, Modality.TEXT, security=security,
        payload_id="p", source_ref="file:///x", event_time=EVENT_TIME,
    )
    units = submit.call_args.kwargs["task"]()
    assert units[0].temporal.t_event == EVENT_TIME

