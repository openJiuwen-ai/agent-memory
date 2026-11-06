"""Protected snapshots and human proof join only on the engine's dedicated return path."""

import copy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    AuthenticationError,
    DeleteMode,
    DeleteSelector,
    PermissionDeniedError,
    assemble_server_runtime,
)
from jiuwen_memory.common.security.privacy.base import PrivacyProtection
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from tests.integration.security.test_memory_privacy import INVITE, SCOPE, TEXT, config, security
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration


@pytest.fixture(params=["in_memory", "cloud"])
def flow(tmp_path, request, monkeypatch):
    calls = []
    engine_type = InMemoryEngine if request.param == "in_memory" else CloudEngine
    original = engine_type.render_human_view

    async def render(privacy, operation, current):
        calls.append(current.content)
        return await original(privacy, operation, current)

    monkeypatch.setattr(engine_type, "render_human_view", staticmethod(render))
    settings = config(tmp_path, human=True)
    settings["engine"] = {
        "default": {
            "target": request.param,
            "params": {"index_builder": "default", "evolver": "default"},
        }
    }
    runtime = assemble_server_runtime(config=settings)
    view, key = runtime.human_view, VirtualPasskey()
    start = view.begin({"invite": INVITE}, "browser")
    view.finish(
        {"challenge_id": start["challenge_id"], "credential": key.response(start, enroll=True)},
        "browser",
    )
    unit = runtime.api.add(TEXT, SCOPE, security=security())[0]
    yield runtime, view, key, unit, calls
    runtime.close()


def begin(flow):
    _, view, _, unit, _ = flow
    return view.begin({"scope": asdict(SCOPE), "unit_id": unit.id}, "browser", security=security())


def finish_payload(start, key):
    return {
        **start["target"],
        "challenge_id": start["challenge_id"],
        "credential": key.response(start, counter=2),
    }


def test_initial_query_supplies_snapshot_and_only_engine_releases_result(flow, monkeypatch):
    runtime, view, key, unit, calls = flow
    privacy = next(
        item for item in view.security_dependencies() if isinstance(item, PrivacyProtection)
    )
    staged = []
    original = privacy.stage_disclosure

    def stage(snapshot):
        staged.append(copy.deepcopy(snapshot))
        return original(snapshot)

    monkeypatch.setattr(privacy, "stage_disclosure", stage)
    start = begin(flow)
    assert staged[0].content == unit.content and "private-demo-value" not in staged[0].content
    assert start["target"]["revision"] == unit.system_metadata["privacy_revision"]
    assert calls == [], "No rendering is allowed before human proof"
    result = view.finish(finish_payload(start, key), "browser")
    assert result == {"unit_id": unit.id, "content": TEXT}
    assert calls == [unit.content], "The dedicated engine path receives only protected truth"
    assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
    with pytest.raises(AuthenticationError):
        view.finish(finish_payload(start, key), "browser")
    assert len(calls) == 1


def test_begin_requires_primary_identity_and_resource_permission(flow):
    _, view, _, unit, calls = flow
    target = {"scope": asdict(SCOPE), "unit_id": unit.id}
    with pytest.raises(AuthenticationError, match="primary"):
        view.begin(target, "browser")
    from jiuwen_memory.common.type_def import Scope

    with pytest.raises(PermissionDeniedError):
        view.begin(target, "browser", security=security(Scope(org="o", user="bob")))
    assert calls == []


@pytest.mark.parametrize("tamper", ["browser", "target", "proof"])
def test_failed_attempt_consumes_snapshot_without_rendering(flow, tamper):
    _, view, key, _, calls = flow
    start = begin(flow)
    payload = finish_payload(start, key)
    browser = "browser"
    if tamper == "browser":
        browser = "other-browser"
    elif tamper == "target":
        payload["unit_id"] = "other-memory"
    else:
        payload["credential"]["response"]["signature"] = "AAAA"
    with pytest.raises(AuthenticationError):
        view.finish(payload, browser)
    with pytest.raises(AuthenticationError):
        view.finish(finish_payload(start, key), "browser")
    assert calls == []


def test_deletion_during_verification_cannot_release_staged_sentence(flow):
    runtime, view, key, unit, calls = flow
    start = begin(flow)
    runtime.api.delete(
        DeleteSelector(scope=SCOPE, unit_ids=[unit.id], mode=DeleteMode.PURGE), security=security()
    )
    from jiuwen_memory.api import AgentMemoryError

    with pytest.raises(AgentMemoryError):
        view.finish(finish_payload(start, key), "browser")
    assert calls == []


def test_expired_operation_requires_new_begin(flow, monkeypatch):
    from jiuwen_memory.common.security.privacy import disclosure

    _, view, key, _, calls = flow
    start = begin(flow)
    after_expiry = disclosure.time.monotonic() + 301
    monkeypatch.setattr(disclosure, "time", SimpleNamespace(monotonic=lambda: after_expiry))
    with pytest.raises(AuthenticationError):
        view.finish(finish_payload(start, key), "browser")
    assert calls == []


def test_revocation_between_begin_and_finish_blocks_disclosure(flow):
    from jiuwen_memory.api import Action, Grant, Scope

    runtime, view, key, _, calls = flow
    owner = Scope(org="o", user="bob")
    unit = runtime.api.add(TEXT, owner, security=security(owner))[0]
    grant = runtime.api.grant(
        Grant(grantor=owner, grantee=SCOPE, actions=frozenset({Action.READ})),
        security=security(owner),
    )
    start = view.begin({"scope": asdict(owner), "unit_id": unit.id}, "browser", security=security())
    runtime.api.revoke(grant, security=security(owner))
    with pytest.raises(PermissionDeniedError):
        view.finish(finish_payload(start, key), "browser")
    assert calls == []
